"""Maintenance Mode: Operator mutations rejected, read access untouched.

An Admin engages it for platform upgrades and maintenance. Apchi engages it itself
after a failed Auto Rollback, so nobody edits a Cluster whose state is unknown --
which is the one case where the state is set by Apchi rather than by a person, and
the reason it is persisted rather than held in memory: a restart must not clear an
incident. See sections 10 and 14.
"""

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.audit.actor import SYSTEM_ACTOR, actor
from app.audit.events import Action, AuditStore

logger = logging.getLogger(__name__)

_DOCUMENT_ID = "maintenance"


class EngagedBy(StrEnum):
    ADMIN = "admin"
    #: Apchi engaged it itself: the Cluster is in an unknown state.
    AUTO_ROLLBACK_FAILURE = "auto_rollback_failure"


class MaintenanceState(BaseModel):
    engaged: bool = False
    reason: str | None = None
    engaged_by: EngagedBy | None = None
    engaged_at: datetime | None = None


class MaintenanceStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]], audit: AuditStore) -> None:
        self._collection = database["maintenance"]
        self._audit = audit

    async def get(self) -> MaintenanceState:
        document = await self._collection.find_one({"_id": _DOCUMENT_ID})
        if document is None:
            return MaintenanceState()
        document.pop("_id", None)
        return MaintenanceState.model_validate(document)

    async def engage(self, reason: str, by: EngagedBy) -> MaintenanceState:
        state = MaintenanceState(
            engaged=True, reason=reason, engaged_by=by, engaged_at=datetime.now(UTC)
        )
        await self._save(state)
        logger.warning("maintenance mode engaged", extra={"by": by.value, "why": reason})
        # Attributed to whoever asked, except when Apchi engaged it itself after a failed
        # Auto Rollback -- which is the case somebody arriving at a frozen Cluster most needs
        # told apart from an Admin freezing it for an upgrade.
        await self._audited("engaged", f"engaged by {by.value}: {reason}", by)
        return state

    async def release(self) -> MaintenanceState:
        state = MaintenanceState()
        await self._save(state)
        logger.warning("maintenance mode released")
        # Releasing it after a failed Auto Rollback is a human saying the Cluster's state is
        # understood again. That is a claim worth attributing.
        await self._audited("released", None, EngagedBy.ADMIN)
        return state

    async def _audited(self, action: Action, detail: str | None, by: EngagedBy) -> None:
        """One event, never fatal.

        Maintenance Mode freezes every Operator's work, and §14 engages it when the Cluster's
        state is unknown. Neither may be prevented by an audit row that will not write, so
        the failure is logged and the state change stands.
        """
        who = SYSTEM_ACTOR if by is EngagedBy.AUTO_ROLLBACK_FAILURE else actor()
        try:
            await self._audit.record(
                who.subject,
                who.via,
                "admin",
                action,
                resource="maintenance_mode",
                detail=detail,
                token_id=who.token_id,
            )
        except PyMongoError:
            logger.exception("could not record a maintenance mode change in the audit history")

    async def _save(self, state: MaintenanceState) -> None:
        await self._collection.replace_one(
            {"_id": _DOCUMENT_ID},
            {"_id": _DOCUMENT_ID, **state.model_dump(mode="json")},
            upsert=True,
        )
