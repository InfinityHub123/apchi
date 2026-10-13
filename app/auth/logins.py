"""The half-finished login: what Apchi has to remember while the browser is at the provider.

An authorization code flow needs three things carried across a redirect it does not control:
the `state` it will compare on the way back, the `nonce` the ID token must echo, and the PKCE
`code_verifier` the token endpoint will ask for. All three are per-attempt secrets, and the
callback is the only place they are ever used.

They live in MongoDB rather than in a signed cookie. Authlib's Starlette client keeps them in
`request.session`, which means Starlette's `SessionMiddleware` and a signing key shared by
every replica -- a second cookie mechanism and a second secret in values, for data that lives
for five minutes. A row costs one read, against the MongoDB Apchi cannot work without, and
pays for itself twice:

* **Replay.** `take` deletes as it reads, so a `state` only ever works once. Refusing a
  replayed callback is then a property of the storage rather than a check somebody wrote.
* **Expiry.** A TTL index reaps attempts nobody completed, so an abandoned login leaves
  nothing behind.

The cookie holds an opaque handle and nothing else, exactly as a session does (`sessions.py`),
which is also what binds the callback to the user agent that started the flow (RFC 6749
§10.12): a `state` lifted from a provider's logs arrives without it.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from pydantic import BaseModel
from pymongo.asynchronous.database import AsyncDatabase

logger = logging.getLogger(__name__)

#: The handle to a login in progress. Deleted the moment the flow finishes either way, so it
#: never coexists with the session cookie it turns into.
COOKIE = "apchi_login"

_HANDLE_BYTES = 32


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class PendingLogin(BaseModel):
    state: str
    nonce: str
    code_verifier: str
    expires_at: datetime


class LoginStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["oidc_logins"]

    async def start(self, login: PendingLogin) -> str:
        """Records one attempt, returning the cookie handle -- which is never stored."""
        handle = secrets.token_urlsafe(_HANDLE_BYTES)
        await self._collection.insert_one({"_id": _digest(handle), **login.model_dump()})
        return handle

    async def take(self, handle: str) -> PendingLogin | None:
        """The attempt, removed. None when it is unknown, already used, or expired.

        Delete-on-read is the point: a second callback carrying the same `state` finds
        nothing, so a replay is refused by construction rather than by a comparison somebody
        has to remember to write.
        """
        document = await self._collection.find_one_and_delete({"_id": _digest(handle)})
        if document is None:
            return None
        login = PendingLogin.model_validate(
            {key: document[key] for key in ("state", "nonce", "code_verifier", "expires_at")}
        )
        expires_at = login.expires_at
        if expires_at.tzinfo is None:
            # MongoDB returns naive UTC datetimes.
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return None
        return login

    async def ensure_indexes(self) -> None:
        """A TTL index, so abandoned logins are reaped rather than accumulating forever.

        `expireAfterSeconds=0` deletes once `expires_at` has passed, which keeps the lifetime
        on the row: changing the setting applies to new attempts without touching the index.
        """
        await self._collection.create_index("expires_at", expireAfterSeconds=0)


def pending(ttl: timedelta) -> PendingLogin:
    """A fresh attempt, with the three per-attempt secrets the flow needs.

    `secrets.token_urlsafe` for all three. The `code_verifier` length is what RFC 7636 §4.1
    asks for -- 43 to 128 characters -- and 32 random bytes lands inside it.
    """
    return PendingLogin(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(32),
        expires_at=datetime.now(UTC) + ttl,
    )
