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
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SectionPlan, ValidationFailure
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

    async def apply(self, cluster: Cluster, desired: Resources, plan: SectionPlan) -> None:
        """Write the file, then make sure it is mounted -- or unmounted.

        The mount is the part that carries the meaning. Trino reads
        `etc/event-listener.properties` if it is there and ignores its absence, but it
        refuses to start if a file it was told to read is missing, and Kubernetes turns a
        subPath mount of an absent Secret key into a *directory*, which Trino chokes on
        with "Is a directory". So "no Event Listener" has to be expressed by there being no
        mount at all, which means Apchi owns this one volume on the coordinator's pod
        template. Everything else there belongs to the Admin.

        Nothing is adopted here. The Rollout is what makes it live, and the pipeline runs
        it because this Section declares `requires_rollout`.
        """
        await cluster.kubernetes.write_secret(
            cluster.settings.event_listener_secret_name, render_secret(desired)
        )
        await self._mount(cluster, present=bool(desired))
        logger.info("delivered the event listener configuration", extra={"listeners": len(desired)})

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Rewriting the file is the whole undo.

        Unlike Catalogs there is no compensating statement to work out: the file engines
        are declarative, so the previous content is simply written again. The restart that
        makes it live is the pipeline's, and it happens within Auto Rollback's single
        bounded attempt.
        """
        current = await cluster.kubernetes.read_secret(cluster.settings.event_listener_secret_name)
        changed = current != render_secret(snapshot)
        await self.apply(cluster, snapshot, self.plan(snapshot, {}))
        return changed

    async def _mount(self, cluster: Cluster, present: bool) -> None:
        settings = cluster.settings
        if present:
            await cluster.kubernetes.mount_secret_file(
                settings.coordinator_deployment_name,
                settings.trino_container_name,
                settings.event_listener_volume_name,
                settings.event_listener_secret_name,
                MOUNT_PATH,
                FILE_KEY,
            )
        else:
            await cluster.kubernetes.unmount_secret_file(
                settings.coordinator_deployment_name,
                settings.trino_container_name,
                settings.event_listener_volume_name,
                MOUNT_PATH,
            )

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        return []

    def needs_probe(self, desired: Resources) -> bool:
        return False

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
