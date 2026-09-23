"""The Applies API.

An Apply takes minutes, so POST returns immediately with an identifier and the
work continues in the background. Progress is readable by identifier and
streamable as Server-Sent Events.
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request, status
from fastapi.sse import EventSourceResponse, format_sse_event

from app.api.deps import ApplyStoreDep, CandidateStoreDep, MutationsEnabled
from app.api.errors import Conflict, NotFound
from app.pipeline.applies import ApplyRecord, ApplyRunner

router = APIRouter(prefix="/applies", tags=["applies"])

#: How often the stream re-reads the record. The stream is a view over durable
#: state, so it polls MongoDB rather than holding progress in memory -- which is
#: what lets a different process serve a reconnect.
POLL_SECONDS = 0.25


@router.post(
    "",
    response_model=ApplyRecord,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Promote the Configuration Candidate",
    # The freeze is checked in the handler rather than through the shared gate, so the
    # refusal can name the Apply already in flight.
    dependencies=[MutationsEnabled],
)
async def start_apply(
    request: Request, applies: ApplyStoreDep, candidate_store: CandidateStoreDep
) -> ApplyRecord:
    if (running := await applies.in_flight()) is not None:
        raise Conflict(
            f"Apply {running.id} is already in flight. The Candidate is frozen until it finishes."
        )
    candidate = await candidate_store.load()
    record = await applies.create(base_snapshot=candidate.base_snapshot)
    runner: ApplyRunner = request.app.state.apply_runner
    runner.start(record)
    return record


@router.get("", response_model=list[ApplyRecord], summary="Past and current Applies")
async def list_applies(applies: ApplyStoreDep) -> list[ApplyRecord]:
    return await applies.list()


@router.get("/{apply_id}", response_model=ApplyRecord, summary="One Apply's progress")
async def get_apply(apply_id: str, applies: ApplyStoreDep) -> ApplyRecord:
    record = await applies.get(apply_id)
    if record is None:
        raise NotFound(f"No Apply with id {apply_id!r}.")
    return record


@router.get("/{apply_id}/events", summary="Stream stage transitions")
async def stream_apply(
    apply_id: str,
    applies: ApplyStoreDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """Streams every stage transition, replaying the ones already past.

    A client that reconnects sends Last-Event-ID; everything after it is replayed,
    so a dropped connection does not leave a blank timeline.
    """
    record = await applies.get(apply_id)
    if record is None:
        raise NotFound(f"No Apply with id {apply_id!r}.")

    async def events() -> AsyncIterator[bytes]:
        sent = int(last_event_id) + 1 if last_event_id is not None else 0
        while True:
            current = await applies.get(apply_id)
            if current is None:
                return
            for index in range(sent, len(current.history)):
                event = current.history[index]
                yield format_sse_event(
                    id=str(index),
                    event="stage",
                    data_str=json.dumps(
                        {
                            "stage": event.stage.value,
                            "at": event.at.isoformat(),
                            "detail": event.detail,
                            "failure_reason": current.failure_reason,
                        }
                    ),
                )
                sent = index + 1
            if not current.in_flight:
                return
            await asyncio.sleep(POLL_SECONDS)

    return EventSourceResponse(events())
