"""The Event Listeners Section: the operations the pipeline and the API share.

Rollout-required, and the first Section that is. `EventListenerManager.loadEventListeners()`
is guarded by a `compareAndSet` permitting exactly one call per process lifetime and there
is no reload path, so Trino adopts a listener change only by restarting. See section 13.6.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.adapters.trino import Trino
from app.api.errors import Conflict, NameAlreadyTaken, NotFound, UnprocessablePayload
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorFile,
    Resources,
    SectionPlan,
    ValidationFailure,
)
from app.sections.event_listeners import SECTION
from app.sections.event_listeners.generator import FILE_KEY, MOUNT_PATH, render_secret
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
    listener_type = update.type or current["type"]
    raw = current["properties"] if update.properties is None else update.properties
    stored[name] = {"type": listener_type, "properties": _validated(listener_type, raw)}
    return _as_listener(name, stored[name])


def delete_event_listener(stored: Resources, name: str) -> None:
    if name not in stored:
        raise NotFound(f"No Event Listener named {name!r} in the Configuration Candidate.")
    del stored[name]


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

    def coordinator_file(self, settings: Settings) -> CoordinatorFile:
        """Where Trino reads the listener configuration, and the Secret Apchi puts it in.

        The path is Trino's default, not a choice. `event-listener.config-files` would let
        it live anywhere, but it makes Trino refuse to start when the file it names is
        missing -- and a Section that cannot be empty is not optional.
        """
        return CoordinatorFile(
            secret=settings.event_listener_secret_name,
            volume=settings.event_listener_volume_name,
            path=MOUNT_PATH,
        )

    def render_file(self, desired: Resources, settings: Settings, admin: AdminValues) -> str | None:
        return render_secret(desired).get(FILE_KEY)

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
        return []

    def needs_probe(self, desired: Resources) -> bool:
        return bool(desired)

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        return []

    async def verify(self, cluster: Cluster, desired: Resources) -> list[str]:
        """Nothing to assert.

        An event listener's output goes to an external sink rather than back to Apchi, so
        there is no functional probe for whether it loaded, and section 8 is explicit that
        Verification is functional rather than introspective. What Verification does prove
        is that the Cluster came back from the Rollout at all, which is the failure that
        actually matters here.
        """
        return []
