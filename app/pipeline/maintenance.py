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
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["maintenance"]

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
        return state

    async def release(self) -> MaintenanceState:
        state = MaintenanceState()
        await self._save(state)
        logger.warning("maintenance mode released")
        return state

    async def _save(self, state: MaintenanceState) -> None:
        await self._collection.replace_one(
            {"_id": _DOCUMENT_ID},
            {"_id": _DOCUMENT_ID, **state.model_dump(mode="json")},
            upsert=True,
        )
