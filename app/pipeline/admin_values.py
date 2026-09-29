"""Where Admin values live.

One document, read at the start of every Apply and held for the whole of it. An Admin
changing something mid-Apply must not change what that Apply is delivering, for the same
reason the Candidate is frozen: the pipeline's guarantees depend on nothing moving
underneath it (invariant 4).
"""

import logging
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.sections.admin import AdminValues

logger = logging.getLogger(__name__)

_DOCUMENT_ID = "admin"


class AdminStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["admin_values"]

    async def load(self) -> AdminValues:
        document = await self._collection.find_one({"_id": _DOCUMENT_ID})
        if document is None:
            return AdminValues()
        document.pop("_id", None)
        return AdminValues.model_validate(document)

    async def save(self, values: AdminValues) -> AdminValues:
        """Replaces the document. Admin values are small and few, and a partial update of
        a list is a worse contract than sending the list you want."""
        await self._collection.replace_one(
            {"_id": _DOCUMENT_ID},
            {"_id": _DOCUMENT_ID, **values.model_dump(mode="json")},
            upsert=True,
        )
        logger.info(
            "admin values saved",
            extra={"preserved_mappings": len(values.preserved_certificate_mappings)},
        )
        return values
