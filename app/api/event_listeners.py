"""Event Listeners. Every change lands in the Configuration Candidate and reaches nothing
else -- not Trino, not Kubernetes. Apply is what makes a change real.

Unlike Catalogs, this Section is rollout-required: applying a change here restarts the
coordinator and terminates every running query. Nothing in this module does that, and an
Apply with an Event Listener staged is refused at Validation until delivery exists.
"""

from typing import Annotated

from fastapi import APIRouter, Body, status

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SettingsDep,
    SnapshotStoreDep,
)
from app.pipeline.recovery import RevertEffect, RevertRequest, section_revert
from app.sections.event_listeners import SECTION as LISTENERS
from app.sections.event_listeners import section
from app.sections.event_listeners.model import (
    EventListener,
    EventListenerUpdate,
    EventListenerWrite,
)

router = APIRouter(prefix="/event-listeners", tags=["event listeners"])

#: Two, for the same reason the Catalog ones are three: `properties` **replaces** the map
#: rather than merging into it, and that is invisible in any single body. Getting it wrong
#: here is quieter than elsewhere -- a listener with a property dropped still loads, and
#: simply stops reporting what it was configured to report.
UPDATE_EXAMPLES = {
    "properties": {
        "summary": "Change one property, resend the rest",
        "description": (
            "Every property this listener should keep has to be in the body. Omitting "
            "`log-created` here is not 'leave it alone', it is 'remove it' -- and Trino "
            "then falls back to the listener's own default rather than failing."
        ),
        "value": {
            "properties": {
                "http-event-listener.connect-ingest-uri": (
                    "http://audit.observability.svc.cluster.local:8080/trino-v2"
                ),
                "http-event-listener.log-completed": "true",
                "http-event-listener.log-created": "false",
            }
        },
    },
    "type": {
        "summary": "Change the listener type",
        "description": (
            "It cannot be cleared: a listener without a type is not a listener. Omit the "
            "field to keep the current one. Changing it almost certainly means changing "
            "`properties` in the same request, since the property names are the type's."
        ),
        "value": {
            "type": "kafka",
            "properties": {
                "kafka-event-listener.broker-endpoints": "kafka.svc.cluster.local:9092",
                "kafka-event-listener.client-id": "trino-apchi",
                "kafka-event-listener.created-event.topic": "trino-queries-created",
                "kafka-event-listener.completed-event.topic": "trino-queries-completed",
                "kafka-event-listener.publish-created-event": "false",
            },
        },
    },
}


@router.post(
    "/revert",
    response_model=RevertEffect,
    summary="Revert one Section to an earlier Snapshot",
    dependencies=[OperatorMutationAllowed],
)
async def revert(
    request: RevertRequest,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> RevertEffect:
    """Stages into the Candidate; it does not apply. An ordinary POST /applies follows,
    like any other edit -- and because this Section is rollout-required, that Apply
    restarts the coordinator. The effect says so, and what it costs.
    To take **every** Section back to one Snapshot instead of just this one, use
    `POST /candidate/rollback` -- a Full Rollback.
    """
    return await section_revert(store, snapshots, cluster, LISTENERS, request.snapshot)


@router.get("", response_model=list[EventListener], summary="List staged Event Listeners")
async def list_event_listeners(store: CandidateStoreDep) -> list[EventListener]:
    return section.list_event_listeners((await store.load()).resources(LISTENERS))


@router.post(
    "",
    response_model=EventListener,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a new Event Listener",
    dependencies=[OperatorMutationAllowed],
)
async def create_event_listener(
    write: EventListenerWrite, store: CandidateStoreDep
) -> EventListener:
    candidate = await store.load()
    created = section.create_event_listener(candidate.resources(LISTENERS), write)
    await store.save(candidate)
    return created


@router.get("/{name}", response_model=EventListener, summary="Fetch a staged Event Listener")
async def get_event_listener(name: str, store: CandidateStoreDep) -> EventListener:
    return section.get_event_listener((await store.load()).resources(LISTENERS), name)


@router.patch(
    "/{name}",
    response_model=EventListener,
    summary="Edit a staged Event Listener",
    dependencies=[OperatorMutationAllowed],
)
async def update_event_listener(
    name: str,
    update: Annotated[EventListenerUpdate, Body(openapi_examples=UPDATE_EXAMPLES)],
    store: CandidateStoreDep,
) -> EventListener:
    candidate = await store.load()
    updated = section.update_event_listener(candidate.resources(LISTENERS), name, update)
    await store.save(candidate)
    return updated


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a staged Event Listener",
    dependencies=[OperatorMutationAllowed],
)
async def delete_event_listener(name: str, store: CandidateStoreDep, settings: SettingsDep) -> None:
    """Refused when it would empty the Section and this Cluster's deployment sets
    `event-listener.config-files` -- that property names a file Trino will not start
    without, and there is no listener that does nothing to render in its place (#125)."""
    candidate = await store.load()
    listeners = candidate.resources(LISTENERS)
    section.delete_event_listener(listeners, name)
    section.refuse_if_emptying(listeners, settings)
    await store.save(candidate)
