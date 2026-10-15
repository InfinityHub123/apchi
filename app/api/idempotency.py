"""`Idempotency-Key`: a retry must not do the work twice (§19).

§19 names `POST /applies`, catalog creation, certificate upload and rollback. They are not
equally at risk, which is worth knowing because it explains what this is really protecting:

* **`POST /applies` is the dangerous one.** It already refuses while an Apply is in flight,
  so the window is a retry arriving after the first *finished*: that starts a second Apply,
  which means a second rollout, a second Snapshot and running queries terminated (§7.4).
* The creates already refuse a duplicate name with a 409. Safe, but a retrying client gets an
  error for work it actually succeeded at and cannot tell its own retry from somebody else
  taking the name.
* Rollback stages deterministically from a Snapshot, so it is already idempotent in effect.

**Opt-in**, and that is not timidity. The header is the only thing that can distinguish a
deliberate retry from a second, genuinely intended request -- two Applies in a row is a
reasonable thing to want -- so without it nothing changes.

### Why a dependency *and* a middleware

Replaying a response means storing it, and only a middleware sees what a handler returned.
But deciding *whether* to replay needs the caller, and a middleware runs before routing, so
the identity has not been resolved yet. So the work is split where the information is:

* the **dependency** resolves the caller, claims the key, and short-circuits a replay
* the **middleware** records the response once the handler has produced one

The middleware only acts when the dependency left a claim on `request.state`, which also
keeps it away from the SSE stream at `/applies/{id}/events` -- draining a response body
iterator there would hang until the stream ended.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.api.errors import (
    REQUEST_ID_HEADER,
    ApchiError,
    Conflict,
    UnprocessablePayload,
    request_id,
)

logger = logging.getLogger(__name__)

HEADER: Final = "Idempotency-Key"

#: The methods that do work worth not doing twice. A GET repeated is a GET.
MUTATIONS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths whose response body is **never** stored, so a replay cannot return it.
#:
#: `POST /tokens` answers with a secret Apchi deliberately never keeps -- it stores a digest
#: (#131), and the secret is shown once. Caching the response to replay it would undo that
#: design and make this collection the one place a live token value exists.
#:
#: The claim still happens, which is the part that matters: a retry does not mint a second
#: credential. It is told plainly that the secret cannot be replayed, which is true and more
#: useful than a second token nobody expected.
UNREPLAYABLE: Final[frozenset[str]] = frozenset({"/api/v1/tokens"})

_MAX_STORED_BYTES: Final = 256 * 1024


class ReplayedRequest(ApchiError):
    """Not an error. The way a dependency hands a stored response back to the caller.

    Carries the recorded status and body so the handler is never reached -- which is the whole
    point: the work was already done, and doing it again is what this exists to prevent.
    """

    code = "idempotent_replay"

    def __init__(self, status: int, body: bytes, media_type: str | None) -> None:
        super().__init__("This request was already processed.")
        self.status_code = status
        self.body = body
        self.media_type = media_type


class KeyReused(UnprocessablePayload):
    """The same key with a different request. Refused rather than replayed, because the
    caller believes those two are the same call and exactly one of them is wrong."""

    code = "idempotency_key_reused"


class RequestInFlight(Conflict):
    """The first call with this key has not finished. Told apart from a replay on purpose: a
    client that waits and retries will get the recorded response, where one told "already
    processed" would stop and never collect it."""

    code = "idempotent_request_in_flight"


class SecretNotReplayable(Conflict):
    """The work was done and its response cannot be shown again."""

    code = "idempotent_secret_not_replayable"


@dataclass(frozen=True)
class Claim:
    """The outcome of trying to claim a key."""

    identifier: str
    replay: ReplayedRequest | None = None


def _digest(*parts: str | bytes) -> str:
    sha = hashlib.sha256()
    for part in parts:
        sha.update(part if isinstance(part, bytes) else part.encode())
        sha.update(b"\0")
    return sha.hexdigest()


class IdempotencyStore:
    """Claimed keys and the responses they produced.

    Keyed by **actor and key together**: two Operators picking the same key must not collide,
    and must certainly not be shown each other's responses.
    """

    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["idempotency"]
        self._indexed = False

    async def claim(self, actor: str, key: str, fingerprint: str, ttl: timedelta) -> Claim:
        """Claim the key, or report what the first call with it did.

        The insert *is* the lock. A unique `_id` means two simultaneous requests cannot both
        believe they are the first, without a read-then-write that races exactly when it
        matters -- which is a retry arriving while the original is still running.
        """
        await self._indexed_once()
        identifier = _digest(actor, key)
        try:
            await self._collection.insert_one(
                {
                    "_id": identifier,
                    "fingerprint": fingerprint,
                    "created_at": datetime.now(UTC),
                    "status": None,
                    "body": None,
                    "media_type": None,
                }
            )
        except DuplicateKeyError:
            return self._outcome(await self._collection.find_one({"_id": identifier}), fingerprint)
        return Claim(identifier)

    @staticmethod
    def _outcome(document: dict[str, Any] | None, fingerprint: str) -> Claim:
        if document is None:
            # Expired between the failed insert and this read. Treat it as a fresh request:
            # the alternative is refusing a caller because a TTL fired mid-flight.
            return Claim(_digest("expired", fingerprint))
        if document["fingerprint"] != fingerprint:
            raise KeyReused(
                f"The {HEADER} on this request was already used for a different request. A key "
                "identifies one call, so reusing it means one of the two is a mistake.",
                details=[{"header": HEADER, "problem": "already used for a different request"}],
            )
        if document["status"] is None:
            raise RequestInFlight(
                f"The first request with this {HEADER} has not finished yet. Retry to collect "
                "its response; it will not be run twice."
            )
        if document["body"] is None:
            raise SecretNotReplayable(
                "This request was already processed, and its response cannot be shown again "
                "because it contained a credential Apchi does not store. Nothing was done "
                "twice. If the credential was lost, revoke it and create another."
            )
        return Claim(
            str(document["_id"]),
            ReplayedRequest(
                int(document["status"]),
                str(document["body"]).encode(),
                document.get("media_type"),
            ),
        )

    async def record(
        self, identifier: str, status: int, body: bytes | None, media_type: str | None
    ) -> None:
        """Store what the handler produced, so a retry gets it instead of doing the work.

        Only on success. A failed call records nothing and releases the key, because a retry
        after a 500 should be allowed to actually run -- replaying a failure would make the
        key a way to get permanently stuck.
        """
        if status >= 400:
            await self._collection.delete_one({"_id": identifier})
            return
        stored = None if body is None or len(body) > _MAX_STORED_BYTES else body.decode()
        await self._collection.update_one(
            {"_id": identifier},
            {"$set": {"status": status, "body": stored, "media_type": media_type}},
        )

    async def release(self, identifier: str) -> None:
        """Let go of a key whose request never completed, so it can be retried."""
        await self._collection.delete_one({"_id": identifier})

    async def _indexed_once(self) -> None:
        """Once per process, on first use. Same reasoning as `AuditStore` (#69, #131): tier 1
        starts an app per test, and an index round trip per start is a cost with no reader on
        a collection that is usually empty."""
        global _ENSURED
        if _ENSURED or self._indexed:
            return
        await self.ensure_indexes(timedelta(hours=24))
        _ENSURED = True
        self._indexed = True

    async def ensure_indexes(self, ttl: timedelta) -> None:
        """A TTL index, because a key is only interesting for as long as a retry might come."""
        await self._collection.create_index(
            "created_at", expireAfterSeconds=int(ttl.total_seconds())
        )


_ENSURED = False


def fingerprint(method: str, path: str, body: bytes) -> str:
    """What makes two calls the same call.

    The body is hashed rather than stored: this exists to notice a key being reused for a
    different request, which a digest answers, and keeping payloads would mean keeping the
    credentials inside them.
    """
    return _digest(method, path, body)


def install(app: FastAPI) -> None:
    """The two halves that cannot live in a dependency: rendering a replay, and recording a
    response.

    Both live here rather than in `errors.install` because `errors` must not import this
    module -- this one imports its error types, and the cycle is not worth breaking for the
    sake of keeping every handler in one file.
    """

    @app.exception_handler(ReplayedRequest)
    async def replayed(request: Request, exc: ReplayedRequest) -> Response:
        """A recorded response, returned byte for byte.

        Not rendered as an error, because it is not one: this is what the first call with this
        key answered, and the caller asked for the same thing twice. The header says so, so a
        client can tell a replay from a fresh success without comparing bodies.
        """
        logger.info("replayed an idempotent request", extra={"replayed_status": exc.status_code})
        return Response(
            content=exc.body,
            status_code=exc.status_code,
            media_type=exc.media_type,
            headers={REQUEST_ID_HEADER: request_id(request), "Idempotent-Replay": "true"},
        )

    @app.middleware("http")
    async def record_idempotent_response(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        identifier = getattr(request.state, "idempotency_id", None)
        if identifier is None:
            # No claim, so nothing to record -- and nothing to drain. That guard is what keeps
            # this away from the SSE stream at /applies/{id}/events, where consuming the body
            # iterator would block until the stream ended.
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        store: IdempotencyStore = request.app.state.idempotency_store
        replayable = request.url.path not in UNREPLAYABLE
        try:
            await store.record(
                identifier, response.status_code, body if replayable else None, response.media_type
            )
        except Exception:
            # The work is done and the caller is owed its answer. A failure to record means a
            # retry runs again, which is the behaviour this feature improves on rather than
            # depends on.
            logger.exception("could not record an idempotent response")

        # Rebuilt from the exact bytes rather than re-serialised, so content-length and the
        # media type still describe what is being sent.
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
