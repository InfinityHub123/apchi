"""Browser sessions: a row in MongoDB, not a signed cookie.

A signed stateless cookie would make logout a lie until the cookie expired, and would keep a
user who has just been disabled working for as long as their cookie lasted. Both are the
opposite of what a control plane wants from "revoke this person now", and a row that can be
deleted makes both immediate.

The cost is a database read per request, which is the same read every other route already
makes, against the MongoDB Apchi cannot work without anyway.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

from app.auth.identity import Role

logger = logging.getLogger(__name__)

#: The cookie a browser carries. Named for the product so it cannot collide with another
#: application's on a shared host.
COOKIE = "apchi_session"

#: Hashed, like a token: a session id is a bearer credential, and a database somebody can
#: read should not hand them every live session.
_DIGEST_BYTES = 32


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class Session(BaseModel):
    username: str
    role: Role
    expires_at: datetime


class SessionStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["sessions"]

    async def create(self, username: str, role: Role, ttl: timedelta) -> str:
        """A new session, returning the cookie value -- which is never stored."""
        value = secrets.token_urlsafe(_DIGEST_BYTES)
        await self._collection.insert_one(
            {
                "_id": _digest(value),
                "username": username,
                "role": role,
                "expires_at": datetime.now(UTC) + ttl,
            }
        )
        return value

    async def read(self, value: str) -> Session | None:
        """The session, or None when it is unknown or expired.

        Expiry is checked here as well as by the TTL index, because that index deletes on
        its own schedule -- roughly every minute -- so a row can outlive its expiry by
        longer than anyone would accept from a logout.
        """
        document = await self._collection.find_one({"_id": _digest(value)})
        if document is None:
            return None
        session = Session.model_validate(
            {key: document[key] for key in ("username", "role", "expires_at")}
        )
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            # MongoDB returns naive UTC datetimes.
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return None
        return session

    async def delete(self, value: str) -> None:
        await self._collection.delete_one({"_id": _digest(value)})

    async def ensure_indexes(self) -> None:
        """A TTL index, so expired sessions are reaped rather than accumulating forever.

        `expireAfterSeconds=0` means "delete when `expires_at` has passed", which keeps the
        lifetime on the row rather than in the index -- so changing the TTL setting applies
        to new sessions without touching the index.
        """
        await self._collection.create_index("expires_at", expireAfterSeconds=0)
