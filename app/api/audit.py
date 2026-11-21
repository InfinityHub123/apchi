"""Reading the audit history.

**Operators read this, not only Admins.** The trail is what makes a shared Configuration
Candidate acceptable (ADR-0002): an Operator about to Apply needs to see whose changes they
are shipping, and §21 has to answer "has the Candidate uncommitted changes, and by whom".
Restricting it to Admins would keep the concurrency trade and remove the thing that justified
it.

Two routes, and the second is the first without the paging. They share one set of query
parameters -- declared once, as a dependency -- because an export that filtered differently
from the listing would be a second, undocumented query language over the same collection.
"""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import AuditStoreDep, CallerDep
from app.api.errors import UnprocessablePayload
from app.api.pages import BadCursor, Page
from app.audit.events import Action, AuditEvent, AuditStore, Category, Filters, Outcome

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])

#: NDJSON rather than CSV (#215). One event per line, each line exactly the `AuditEvent` the
#: listing returns, so `openapi.json` documents the export's contents for free and nothing
#: here has to decide a column order or how to flatten a null -- a fixed column order is a
#: contract nobody meant to publish, and CSV would lose the types of `at` and `snapshot`.
NDJSON = "application/x-ndjson"


def audit_filters(
    category: Category | None = None,
    actor: str | None = None,
    outcome: Annotated[
        Outcome | None,
        Query(
            description=(
                "`refused` is the one people come here for: a frozen Candidate, Maintenance "
                "Mode, a rejected payload. It is Apchi working correctly, which is why it is "
                "recorded and why it is worth being able to ask for on its own."
            )
        ),
    ] = None,
    resource: Annotated[
        str | None,
        Query(
            description=(
                "Exact match on the resource's name, not a search. Null on an event about a "
                "whole Section or Cluster, and such an event is therefore never returned by "
                "this filter."
            )
        ),
    ] = None,
    action: Annotated[Action | None, Query()] = None,
    apply_id: Annotated[
        str | None,
        Query(
            description=(
                "Every event belonging to one Apply. The same id is on the log records for "
                "that Apply, so the trail and the logs join on it rather than by timestamp."
            )
        ),
    ] = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Filters:
    """The filters both routes offer, declared once.

    A dependency rather than a Pydantic model read straight from the query string, because
    the descriptions above are most of what makes these parameters usable and they belong
    next to the HTTP surface rather than in the store's model.
    """
    return Filters(
        category=category,
        actor=actor,
        outcome=outcome,
        resource=resource,
        action=action,
        apply_id=apply_id,
        since=since,
        until=until,
    )


FiltersDep = Annotated[Filters, Depends(audit_filters)]


@router.get("", response_model=Page[AuditEvent], summary="Who changed what, newest first")
async def list_events(
    audit: AuditStoreDep,
    filters: FiltersDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    before: str | None = Query(
        default=None, description="A `next` from a previous page. Omit for the newest page."
    ),
) -> Page[AuditEvent]:
    """Answers *who changed what, and when*, which is a different question from what
    `GET /snapshots` answers.

    Events never carry configuration *values* -- only which resource changed. Snapshots hold
    the contents, and hold real credentials to do it (§12), so a second copy here would be a
    second thing to protect.
    """
    try:
        return await audit.page(filters, limit=limit, before=before)
    except BadCursor as bad:
        raise UnprocessablePayload(
            "That `before` value did not come from a previous page's `next`. Omit it to "
            "start from the newest events.",
            details=[{"property": "before", "problem": "not a cursor this endpoint issued"}],
        ) from bad


def _ndjson(audit: AuditStore, filters: Filters) -> AsyncIterator[bytes]:
    """One line per event, as they arrive from MongoDB.

    A generator rather than a list: retention is off by default, so the trail is unbounded
    and building the whole answer first would make Apchi's own history the way to exhaust its
    memory.
    """

    async def lines() -> AsyncIterator[bytes]:
        async for event in audit.stream(filters):
            yield event.model_dump_json().encode() + b"\n"

    return lines()


@router.get(
    "/export",
    summary="Download the whole filtered trail",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "One `AuditEvent` per line, newest first, as NDJSON. The same events the "
                "listing returns for the same filters, with no limit."
            ),
            "content": {NDJSON: {"schema": {"type": "string"}}},
        }
    },
)
async def export(audit: AuditStoreDep, who: CallerDep, filters: FiltersDep) -> StreamingResponse:
    """The listing without the paging, for a trail too long to page through.

    Not Admin-only, for the reason this module exists: the listing is Operator-readable
    deliberately, and the same data in a different shape does not deserve a different gate.
    It carries exactly what the listing carries -- no configuration values, so the file is
    not a new thing to protect (§12).

    **The export is itself audited, and the listing is not.** Taking a copy of the whole
    trail leaves Apchi with something that outlives Apchi's own rules about it, which reading
    a page in a console does not; §20's own logic -- record what somebody would later need to
    ask about -- gives different answers for the two.

    Recorded *before* the first byte is written, which has two consequences worth naming. A
    client that hangs up mid-stream still leaves the record, because a partial copy of an
    audit trail is exactly the copy you would want to know about. And the copy therefore
    contains the record of itself being taken, which is the right way round.
    """
    await audit.record(
        who.subject,
        who.via,
        "audit",
        "exported",
        detail=filters.described(),
        token_id=who.token_id,
        token_name=who.token_name,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return StreamingResponse(
        _ndjson(audit, filters),
        media_type=NDJSON,
        headers={"Content-Disposition": f'attachment; filename="apchi-audit-{stamp}.ndjson"'},
    )
