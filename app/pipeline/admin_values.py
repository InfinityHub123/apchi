"""Where Admin values live.

One document, read at the start of every Apply and held for the whole of it. An Admin
changing something mid-Apply must not change what that Apply is delivering, for the same
reason the Candidate is frozen: the pipeline's guarantees depend on nothing moving
underneath it (invariant 4).
"""

import json
import logging
from typing import Any

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.audit.actor import actor
from app.audit.events import AuditStore, Entry
from app.sections.admin import AdminValues

logger = logging.getLogger(__name__)

_DOCUMENT_ID = "admin"

#: The document `save` replaced. What a rollback restores only until an Apply has succeeded
#: since this was introduced -- see `_APPLIED_ID`.
_PREVIOUS_ID = "admin_previous"

#: The Admin values the last successful Apply delivered: what the Cluster has. A rollback
#: restores this, because "one write back" is not "what the Cluster had" -- an Admin who wrote
#: a raw file and then a property between two Applies would have had only the property undone.
_APPLIED_ID = "admin_applied"


#: The one Admin value whose *content* reaches the audit. It is a policy switch rather than
#: configuration: it decides whether a Trino Identity may reach only what it has been granted,
#: and whether somebody turned enforcement off is the entire reason to audit it. Everything
#: else in `AdminValues` is content that may carry a credential, so only the key name is
#: recorded -- §14 invariant 9 keeps Admin values out of Snapshots, and the audit must not
#: become the back door that records them.
_POLICY_FLAGS = ("enforce_permissions",)


class AdminStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]], audit: AuditStore) -> None:
        self._collection = database["admin_values"]
        self._audit = audit

    async def load(self) -> AdminValues:
        document = await self._collection.find_one({"_id": _DOCUMENT_ID})
        if document is None:
            return AdminValues()
        document.pop("_id", None)
        return AdminValues.model_validate(document)

    async def save(self, values: AdminValues) -> AdminValues:
        """Replaces the document, and records who changed which value (§20).

        Admin values are small and few, and a partial update of a list is a worse contract
        than sending the list you want.

        **This is the only record an Admin change leaves anywhere.** §14 invariant 9 keeps
        Admin values out of Snapshots deliberately, and §12 makes the Snapshot the record of
        what was committed -- so without this there is no trace of the one class of change
        that can alter the Cluster without appearing in any Snapshot. Written here rather
        than in the five routes above it, for the same reason the Candidate's audit is written
        in one place.
        """
        dumped = values.model_dump(mode="json")
        # The pre-image is kept, not just read for the audit diff. Auto Rollback restores
        # what the Cluster had, and for an Admin Apply that is not a Snapshot -- Admin values
        # never enter one (invariant 9). So the only thing a rollback can restore from is the
        # previous document, and a replace that did not keep it leaves nothing to go back to
        # (§14, #158).
        previous = await self._collection.find_one_and_replace(
            {"_id": _DOCUMENT_ID},
            {"_id": _DOCUMENT_ID, **dumped},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
        logger.info(
            "admin values saved",
            extra={"preserved_mappings": len(values.preserved_certificate_mappings)},
        )
        before = _as_values(previous)
        await self._keep(before)
        await self._record(before.model_dump(mode="json"), dumped)
        return values

    async def carry_over_listener(self, listener: dict[str, str]) -> None:
        """Put an Event Listener the retired Section held into the Admin values (#269).

        Into every copy that exists -- the current values, the pre-image and what the last
        Apply delivered -- not through `save`: the Cluster already runs this listener, so it is
        part of each of those states, and a rollback restoring one without it would remove a
        listener nobody removed. Through the model rather than a `$set`, because the file names
        are keys with dots in them, which a MongoDB update path cannot address.
        """
        for document_id in (_DOCUMENT_ID, _PREVIOUS_ID, _APPLIED_ID):
            stored = await self._collection.find_one({"_id": document_id})
            if stored is None and document_id != _DOCUMENT_ID:
                continue
            values = _as_values(stored)
            coordinator = values.server_configuration.coordinator.model_copy(
                update={"event_listener_properties": dict(listener)}
            )
            carried = values.model_copy(
                update={
                    "server_configuration": values.server_configuration.model_copy(
                        update={"coordinator": coordinator}
                    )
                }
            )
            await self._collection.replace_one(
                {"_id": document_id},
                {"_id": document_id, **carried.model_dump(mode="json")},
                upsert=True,
            )
        who = actor()
        await self._audit.record_each(
            who.subject,
            who.via,
            [
                Entry(
                    category="admin",
                    action="updated",
                    resource="server_configuration",
                    detail="carried the Event Listener over from the retired Section",
                )
            ],
            token_id=who.token_id,
        )

    async def _keep(self, previous: AdminValues) -> None:
        """Remember the document this write replaced, for exactly one step back.

        One step, not a history: a rollback undoes the Apply that just failed, and §11 is
        explicit that reaching further back is a Full Rollback through the pipeline rather
        than an undo stack. A second copy is all a rollback can use, so a second copy is all
        there is.
        """
        await self._collection.replace_one(
            {"_id": _PREVIOUS_ID},
            {"_id": _PREVIOUS_ID, **previous.model_dump(mode="json")},
            upsert=True,
        )

    async def record_applied(self, values: AdminValues) -> None:
        """Remember the values an Apply just delivered and verified, as what the Cluster has.

        Never raised: the Cluster is already running these, and an Apply must not be reported
        failed for a bookkeeping write. The cost of losing one is a later rollback reaching
        back further than it should, which is logged here where it happens.
        """
        try:
            await self._collection.replace_one(
                {"_id": _APPLIED_ID},
                {"_id": _APPLIED_ID, **values.model_dump(mode="json")},
                upsert=True,
            )
        except PyMongoError:
            logger.error(
                "could not record the admin values this apply delivered; a later admin "
                "rollback restores an older copy",
                exc_info=True,
            )

    async def restore_previous(self) -> AdminValues:
        """Put the document back to what the Cluster has, and return it.

        **Without rotating the pre-image.** A rollback is not a new write: rotating here
        would make the bad values the thing a second rollback restores, which is the opposite
        of what anybody wants from the word.

        So the pre-image stays where it is, and restoring twice is idempotent -- which it has
        to be, because §10's recovery can run a rollback after an Apchi restart as well as
        inside the Apply that failed.
        """
        previous = await self.previous()
        await self._collection.replace_one(
            {"_id": _DOCUMENT_ID},
            {"_id": _DOCUMENT_ID, **previous.model_dump(mode="json")},
            upsert=True,
        )
        logger.warning("restored the admin values a failed apply replaced")
        return previous

    async def previous(self) -> AdminValues:
        """What a rollback restores: the values the last successful Apply delivered.

        Until an Apply has succeeded since that record was introduced, the document the last
        write replaced -- an install upgrading from before it has no record of what it
        delivered, and falling back to the defaults would wipe its Admin values on the first
        failed Apply. The defaults when neither exists, which is the honest answer for a
        Cluster whose Admin values have never been written.
        """
        applied = await self._collection.find_one({"_id": _APPLIED_ID})
        if applied is not None:
            return _as_values(applied)
        return _as_values(await self._collection.find_one({"_id": _PREVIOUS_ID}))

    async def _record(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        """One event per Admin value that moved, naming the value and never its content.

        No `snapshot`, on any of them: an Admin Apply creates none (§14), which is the
        invariant this records rather than an omission.
        """
        who = actor()
        entries = [
            Entry(
                category="admin",
                action="updated",
                resource=key,
                detail=_direction(key, after[key]),
            )
            for key in sorted(set(before) | set(after))
            if json.dumps(before.get(key), sort_keys=True)
            != json.dumps(after.get(key), sort_keys=True)
        ]
        await self._audit.record_each(
            who.subject, who.via, entries, token_id=who.token_id, token_name=who.token_name
        )


def _as_values(document: dict[str, Any] | None) -> AdminValues:
    """A stored document as values, or the defaults when nothing was stored.

    Through the model rather than compared raw, so both sides of the diff are dumped by the
    same schema: MongoDB returns BSON, and comparing that against freshly dumped JSON would
    report every value as changed on every save.
    """
    if document is None:
        return AdminValues()
    document.pop("_id", None)
    return AdminValues.model_validate(document)


def _direction(key: str, value: Any) -> str | None:
    """What a policy flag was set to, for the flags where that is the point.

    `None` for everything else, because the audit records which Admin value changed and never
    what it changed to -- the rest of `AdminValues` is content that may carry a credential.
    """
    if key in _POLICY_FLAGS and isinstance(value, bool):
        return f"{key} set to {str(value).lower()}"
    return None
