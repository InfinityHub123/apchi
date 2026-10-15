"""The Event Listeners Section: the operations the pipeline and the API share.

Rollout-required, and the first Section that is. `EventListenerManager.loadEventListeners()`
is guarded by a `compareAndSet` permitting exactly one call per process lifetime and there
is no reload path, so Trino adopts a listener change only by restarting. See section 13.6.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.adapters.trino import Trino
from app.api.errors import Conflict, NameAlreadyTaken, NotFound, UnprocessablePayload
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorDirectory,
    CoordinatorFile,
    Delivery,
    DiscoveredPaths,
    Parsed,
    ParseProblem,
    Resources,
    SectionPlan,
    SmokeQuery,
    Unaccounted,
    ValidationFailure,
    unreadable_each,
)
from app.sections.event_listeners import SECTION
from app.sections.event_listeners.generator import (
    FILE_KEY,
    MOUNT_DIR,
    MOUNT_PATH,
    PATH_IN_DIR,
    Unreadable,
    parse_properties,
    render_secret,
)
from app.sections.event_listeners.model import (
    EventListener,
    EventListenerUpdate,
    EventListenerWrite,
)
from app.sections.event_listeners.types import PropertyProblem, is_curated, validate_properties

logger = logging.getLogger(__name__)

#: Trino reads one `etc/event-listener.properties` by default. More than one needs
#: `event-listener.config-files` in `config.properties`, which is Admin-owned
#: configuration Apchi does not write, so the Candidate holds at most one.
MAX_LISTENERS = 1


class TooManyEventListeners(Conflict):
    code = "too_many_event_listeners"


def _as_listener(name: str, stored: dict[str, Any]) -> EventListener:
    return EventListener(
        name=name,
        type=stored["type"],
        properties=stored.get("properties", {}),
        supported=is_curated(stored["type"]),
    )


def list_event_listeners(stored: Resources) -> list[EventListener]:
    return [_as_listener(name, stored[name]) for name in sorted(stored)]


def get_event_listener(stored: Resources, name: str) -> EventListener:
    if name not in stored:
        raise NotFound(f"No Event Listener named {name!r} in the Configuration Candidate.")
    return _as_listener(name, stored[name])


def _validated(listener_type: str, properties: dict[str, Any]) -> dict[str, str]:
    try:
        return validate_properties(listener_type, properties)
    except PropertyProblem as exc:
        raise UnprocessablePayload(
            f"The properties are not valid for the {listener_type!r} event listener.",
            details=list(exc.problems),
        ) from exc


def create_event_listener(stored: Resources, write: EventListenerWrite) -> EventListener:
    if write.name in stored:
        raise NameAlreadyTaken(f"An Event Listener named {write.name!r} already exists.")
    if len(stored) >= MAX_LISTENERS:
        raise TooManyEventListeners(
            "Only one Event Listener can be configured. Trino reads a single event "
            "listener file by default, and configuring more than one needs "
            "`event-listener.config-files`, which is Admin-owned Trino configuration "
            "Apchi does not write. Remove the existing Event Listener first."
        )
    properties = _validated(write.type, write.properties)
    stored[write.name] = {"type": write.type, "properties": properties}
    return _as_listener(write.name, stored[write.name])


def update_event_listener(
    stored: Resources, name: str, update: EventListenerUpdate
) -> EventListener:
    if name not in stored:
        raise NotFound(f"No Event Listener named {name!r} in the Configuration Candidate.")
    current = stored[name]
    # Omitted and null are different answers, read from `model_fields_set` -- the same rule
    # the Catalogs Section follows, because §19 asks for PATCH semantics that are consistent
    # across every endpoint and not merely correct on each (#155). Neither field here is
    # clearable, so null is refused rather than silently treated as "leave it alone".
    sent = update.model_fields_set
    listener_type = update.type if "type" in sent else current["type"]
    raw = update.properties if "properties" in sent else current["properties"]
    if listener_type is None:
        raise UnprocessablePayload(
            "An Event Listener cannot have its type removed. Send a type, or omit the field "
            "to keep the current one.",
            details=[{"property": "type", "problem": "cannot be cleared"}],
        )
    if raw is None:
        raise UnprocessablePayload(
            "An Event Listener cannot have its properties removed. Send the properties you "
            "want, or omit the field to keep the current ones.",
            details=[{"property": "properties", "problem": "cannot be cleared"}],
        )
    stored[name] = {"type": listener_type, "properties": _validated(listener_type, raw)}
    return _as_listener(name, stored[name])


def delete_event_listener(stored: Resources, name: str) -> None:
    if name not in stored:
        raise NotFound(f"No Event Listener named {name!r} in the Configuration Candidate.")
    del stored[name]


#: Why emptying this Section is refused when the deployment sets the pointer, in one place so
#: the request-time refusal and the Apply-time one cannot drift apart.
#:
#: There is no second option here, unlike Resource Groups. Trino ships `http`, `mysql` and
#: `kafka` listeners and every one of them needs real configuration -- a sink to write to --
#: so there is no listener that does nothing to render in place of none at all.
CANNOT_EMPTY = (
    "This Cluster's deployment sets event-listener.config-files, which names a file Trino "
    "refuses to start without. Removing the last Event Listener would leave a coordinator "
    "that cannot come back, and Trino has no listener that does nothing to put in its place. "
    "Remove event-listener.config-files from the deployment first, or keep at least one "
    "listener."
)


def refuse_if_emptying(stored: Resources, settings: Settings) -> None:
    """Refuse a change that would leave this Section with nothing, in that mode only.

    At request time rather than only at Apply, because the Candidate is shared: an empty
    Section that only Validation catches blocks everyone's Apply until somebody works out
    whose edit did it (ADR-0002). `check` enforces the same rule for the paths that do not
    come through here -- a Section Revert, and a whole-document import.
    """
    if not settings.event_listener_config_files_from_deployment:
        return
    if not stored:
        raise Conflict(CANNOT_EMPTY)


@dataclass
class ListenerPlan:
    """What applying would change. Not yet used to change anything."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.changed:
            parts.append(f"~{len(self.changed)}")
        if self.removed:
            parts.append(f"-{len(self.removed)}")
        return " ".join(parts) or "no changes"


class EventListenersSection:
    """The Event Listeners Section as the pipeline sees it.

    Rollout-required, and the first Section that is: Trino loads event listeners exactly
    once per process lifetime, so a change is adopted only by a new pod.
    """

    name: SectionName = SECTION
    #: Trino loads event listeners exactly once per process lifetime.
    requires_rollout = True

    def discover_paths(self, settings: Settings, properties: Mapping[str, str]) -> DiscoveredPaths:
        """Where Trino reads its listeners from, which depends on what points at it.

        By default nothing does: Trino reads `etc/event-listener.properties` if it is there
        and ignores its absence, so there is no property naming it and nowhere else it could
        be. An Admin who sets `event-listener.config-files` anyway has their listeners
        somewhere this cannot see, and the discovery says so by finding nothing here.

        When *this Cluster* is the one whose deployment sets that property (#125), the
        property is read and followed -- it is the Admin's own statement of where the file
        is, and ignoring it would make discovery look in a directory nothing reads.
        """
        if settings.event_listener_config_files_from_deployment:
            named = properties.get("event-listener.config-files")
            if named:
                return DiscoveredPaths(files={PATH_IN_DIR: named})
            return DiscoveredPaths(
                files={PATH_IN_DIR: PATH_IN_DIR},
                why=(
                    "this Cluster is configured for the deployment to set "
                    "event-listener.config-files, and no such property was found -- so this "
                    "is where Apchi would put the file rather than where Trino reads it"
                ),
            )
        return DiscoveredPaths(files={MOUNT_PATH: MOUNT_PATH})

    @staticmethod
    def _path(settings: Settings) -> str:
        """Where Apchi writes the listener file, which depends on who owns the pointer."""
        if settings.event_listener_config_files_from_deployment:
            return PATH_IN_DIR
        return MOUNT_PATH

    def parse_files(self, files: Mapping[str, str], settings: Settings) -> Parsed:
        """An absent file means no listener, which is this Section's whole way of saying so.

        The listener is named after its type, because the file carries no name -- see
        `parse_properties`. An Operator adopting a Cluster can rename it afterwards; what
        matters is that the configuration is not lost.
        """
        where = self._path(settings)
        content = files.get(where)
        if content is None:
            return Parsed()
        try:
            listener_type, properties, unaccounted = parse_properties(where, content)
        except Unreadable as exc:
            raise ParseProblem(exc.path, exc.reason) from exc
        return Parsed(
            resources={listener_type: {"type": listener_type, "properties": properties}},
            unaccounted=tuple(
                Unaccounted(path=where, what=what, content=found) for what, found in unaccounted
            ),
        )

    def read_back(self, resources: Resources) -> list[ValidationFailure]:
        return unreadable_each(SECTION, resources, lambda name: _as_listener(name, resources[name]))

    def plan(self, desired: Resources, current: Resources) -> ListenerPlan:
        return ListenerPlan(
            added=[name for name in sorted(desired) if name not in current],
            changed=[
                name
                for name in sorted(desired)
                if name in current and desired[name] != current[name]
            ],
            removed=sorted(set(current) - set(desired)),
        )

    def coordinator_files(self, settings: Settings) -> tuple[Delivery, ...]:
        """One file Apchi mounts, or one directory the Admin does (#125).

        **Trino's own path** by default, and that is not an arbitrary preference.
        `event-listener.config-files` would let the file live anywhere, but it makes Trino
        refuse to start when the file it names is missing -- so using it would make this
        Section impossible to empty, and a Section that cannot be empty is not optional.
        Trino's convention path is read if present and ignored if absent, which is exactly
        the behaviour an optional Section needs.

        **The deployment sets `event-listener.config-files`** when it mounts the whole of
        `/etc/trino` and so cannot accept a single file inside it. That buys portability at
        the cost named above, which is why it is the Admin's decision and not Apchi's
        default: emptying the Section is then refused rather than silently breaking the
        Cluster on its next restart.
        """
        if settings.event_listener_config_files_from_deployment:
            return (
                CoordinatorDirectory(
                    secret=settings.event_listener_secret_name,
                    path=MOUNT_DIR,
                    probe_config={"event-listener.config-files": PATH_IN_DIR},
                ),
            )
        return (
            CoordinatorFile(
                secret=settings.event_listener_secret_name,
                volume=settings.event_listener_volume_name,
                path=MOUNT_PATH,
            ),
        )

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        content = render_secret(desired).get(FILE_KEY)
        if not content:
            return {}
        where = PATH_IN_DIR if settings.event_listener_config_files_from_deployment else MOUNT_PATH
        return {where: content}

    async def apply(self, cluster: Cluster, desired: Resources, plan: SectionPlan) -> None:
        """Nothing beyond the file, which the pipeline has already delivered.

        The Rollout is what makes it live, and the pipeline runs it because this Section
        declares `requires_rollout`.
        """

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Rewriting the file is the whole undo, and the pipeline has done that too.

        Unlike Catalogs there is no compensating statement to work out: the file engines
        are declarative, so the previous content is simply written again. Whether that
        changed anything -- and so whether the rollback must restart Trino -- is answered
        from the durable copy by the pipeline, because recovery after an Apchi restart has
        no plan to consult.
        """
        return False

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        """The net under `refuse_if_emptying`, for the ways this Section empties that do not
        go through a DELETE: a Section Revert to an empty Snapshot, and a whole-document
        import (#125).

        The **state**, not the transition, and that distinction was got wrong once. Scoping it
        to the transition looks kinder -- an Apply that touches only catalogs would stop being
        blocked by a Section nobody asked about -- but it is unsafe: `files.by_secret` writes
        each Secret whole, so an Apply with this Section empty writes an empty Secret, the
        mounted directory loses the file `event-listener.config-files` names, and the next
        restart leaves a coordinator that cannot come back. Apchi would have deleted the
        Admin's own seeded file on its way past.

        There is also no legitimate state this blocks. Trino will not start at all unless that
        file already exists (§16), so a *running* Cluster in this mode always has a listener --
        and a Candidate that does not is a Cluster waiting to be adopted (§15), not a Cluster
        with no listener. The way out is to configure one or to take the pointer out of the
        deployment, and the message says both.
        """
        if cluster.settings.event_listener_config_files_from_deployment and not desired:
            return [ValidationFailure(section=SECTION, reason=CANNOT_EMPTY)]
        return []

    def needs_probe(self, desired: Resources) -> bool:
        return bool(desired)

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        return []

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """Nothing to assert.

        An event listener's output goes to an external sink rather than back to Apchi, so
        there is no functional probe for whether it loaded, and section 8 is explicit that
        Verification is functional rather than introspective. What Verification does prove
        is that the Cluster came back from the Rollout at all, which is the failure that
        actually matters here.
        """
        return []
