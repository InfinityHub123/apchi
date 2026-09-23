"""Event Listeners. Every change lands in the Configuration Candidate and reaches nothing
else -- not Trino, not Kubernetes. Apply is what makes a change real.

Unlike Catalogs, this Section is rollout-required: applying a change here restarts the
coordinator and terminates every running query. Nothing in this module does that, and an
Apply with an Event Listener staged is refused at Validation until delivery exists.
"""

from fastapi import APIRouter, status

from app.api.deps import CandidateStoreDep, OperatorMutationAllowed
from app.sections.event_listeners import SECTION as LISTENERS
from app.sections.event_listeners import section
from app.sections.event_listeners.model import (
    EventListener,
    EventListenerUpdate,
    EventListenerWrite,
)

router = APIRouter(prefix="/event-listeners", tags=["event listeners"])


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
    name: str, update: EventListenerUpdate, store: CandidateStoreDep
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
async def delete_event_listener(name: str, store: CandidateStoreDep) -> None:
    candidate = await store.load()
    section.delete_event_listener(candidate.resources(LISTENERS), name)
    await store.save(candidate)
