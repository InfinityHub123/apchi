"""Reading the audit history.

**Operators read this, not only Admins.** The trail is what makes a shared Configuration
Candidate acceptable (ADR-0002): an Operator about to Apply needs to see whose changes they
are shipping, and §21 has to answer "has the Candidate uncommitted changes, and by whom".
Restricting it to Admins would keep the concurrency trade and remove the thing that justified
it.
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AuditStoreDep
from app.api.errors import UnprocessablePayload
from app.api.pages import BadCursor, Page
from app.audit.events import Action, AuditEvent, Category, Outcome

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=Page[AuditEvent], summary="Who changed what, newest first")
async def list_events(
    audit: AuditStoreDep,
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
        return await audit.page(
            category=category,
            actor=actor,
            outcome=outcome,
            resource=resource,
            action=action,
            apply_id=apply_id,
            since=since,
            until=until,
            limit=limit,
            before=before,
        )
    except BadCursor as bad:
        raise UnprocessablePayload(
            "That `before` value did not come from a previous page's `next`. Omit it to "
            "start from the newest events.",
            details=[{"property": "before", "problem": "not a cursor this endpoint issued"}],
        ) from bad
