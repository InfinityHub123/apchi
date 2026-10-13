"""Tokens for automations: opaque, revocable, and owned by a person.

Opaque rather than signed, for two reasons that both come from this being one control plane
with one database. A signed token cannot be revoked without a blocklist, and that blocklist is
exactly the state a signed token was supposed to remove -- so an opaque value is strictly
simpler and revocation is a single write.

And a signed token would freeze the role into a claim. This one resolves to its owner on every
request and takes the role that user has **now**, so demoting somebody demotes their
automations and disabling somebody stops them. Discovering otherwise during an incident is the
failure worth designing out.

A token is not a second kind of principal. It belongs to a user, acts as that user, and says
so: an audit record (§20) carries the owner *and* which token, so "the nightly pipeline did
this" and "a person did this" are distinguishable without being different kinds of actor.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.audit.actor import actor
from app.audit.events import Action, AuditStore

logger = logging.getLogger(__name__)

#: Prefixes the secret so it is recognisable as one. Secret scanners key on patterns like
#: this, and so does Apchi's own redaction filter, which otherwise only catches a secret when
#: it is sitting next to a field name that looks secret-bearing.
PREFIX = "apchi_"

#: 256 bits. Enough that guessing is not a threat model, which is what justifies the cheap
#: digest below.
_SECRET_BYTES = 32


def mint() -> str:
    return PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def digest(secret: str) -> str:
    """SHA-256, and deliberately **not** Argon2.

    Argon2 is slow on purpose, which is right for a password -- a human-chosen string with
    little entropy, where every guess must cost something. A token is 256 random bits, so a
    guessing attack is not the risk, and a slow hash buys nothing against it.

    What it would cost is real: this runs on **every authenticated request**, so an Argon2
    verification here is roughly 50ms of CPU an unauthenticated caller can spend by sending a
    made-up bearer token. That is a denial-of-service vector against Apchi, pointed at Apchi,
    installed in the name of security.
    """
    return sha256(secret.encode()).hexdigest()


class Token(BaseModel):
    """One token's metadata. The secret is not here, and cannot be recovered from here."""

    id: str
    owner: str
    name: str = Field(description="What an Operator called it, so they can tell theirs apart.")
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def live(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            # MongoDB hands back naive UTC.
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at > datetime.now(UTC)


class Minted(BaseModel):
    """What minting answers: the metadata, and the secret, this once."""

    token: Token
    secret: str = Field(
        description=(
            "Shown here and never again. Apchi stores a digest, so this cannot be recovered "
            "-- a lost token is replaced rather than looked up."
        )
    )


class TokenStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]], audit: AuditStore) -> None:
        self._collection = database["tokens"]
        self._audit = audit

    @staticmethod
    def _as_token(document: dict[str, Any]) -> Token:
        return Token.model_validate({"id": document["_id"], **document})

    async def create(self, owner: str, name: str, expires_in_days: int | None = None) -> Minted:
        secret = mint()
        token = Token(
            id=uuid4().hex,
            owner=owner,
            name=name,
            created_at=datetime.now(UTC),
            expires_at=(
                None
                if expires_in_days is None
                else datetime.now(UTC) + timedelta(days=expires_in_days)
            ),
        )
        await self._collection.insert_one(
            {
                "_id": token.id,
                "token_digest": digest(secret),
                **token.model_dump(mode="json", exclude={"id"}),
            }
        )
        # `token_name` rather than `name`: logging reserves `name` on a LogRecord and raises
        # KeyError on an attempt to set one, which would make minting fail outright.
        logger.info(
            "token minted",
            extra={"owner": owner, "token": token.id, "token_name": name},
        )
        # Minting is not a configuration change, but it creates a credential that acts as its
        # owner with their current role -- so it is audited, under `admin`, by the token's id
        # and never by its value. The digest is all Apchi keeps of the secret, and the audit
        # must not become the one place the secret itself appears.
        await self._audited("minted", token.id, f"{name!r} for {owner}")
        return Minted(token=token, secret=secret)

    async def find(self, secret: str) -> Token | None:
        """The token this secret belongs to, live or not. The caller decides what to do with
        a revoked or expired one -- which is refuse it, but saying so is the resolver's job."""
        document = await self._collection.find_one({"token_digest": digest(secret)})
        return None if document is None else self._as_token(document)

    async def touch(self, token_id: str) -> None:
        """Record that this token was just used.

        A write on every authenticated request, which is the cost of being able to answer
        "is this automation still running?" -- the question an Operator asks before revoking
        a token nobody claims. Unconditional rather than throttled: a conditional update is
        a read-then-write that can be wrong, and this one is a single indexed `$set`.
        """
        await self._collection.update_one(
            {"_id": token_id}, {"$set": {"last_used_at": datetime.now(UTC)}}
        )

    async def owned_by(self, owner: str) -> list[Token]:
        cursor = self._collection.find({"owner": owner}).sort("created_at", 1)
        return [self._as_token(document) async for document in cursor]

    async def get(self, token_id: str) -> Token | None:
        document = await self._collection.find_one({"_id": token_id})
        return None if document is None else self._as_token(document)

    async def revoke(self, token_id: str) -> bool:
        """Mark it dead. Returns whether it was live before.

        Marked rather than deleted, so a token id in an audit trail still resolves to
        something that says what it was and when it stopped -- the same reason a user is
        disabled rather than removed.
        """
        result = await self._collection.update_one(
            {"_id": token_id, "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(UTC)}},
        )
        revoked = result.modified_count == 1
        if revoked:
            await self._audited("revoked", token_id, None)
        return revoked

    async def _audited(self, action: Action, token_id: str, detail: str | None) -> None:
        """One event, never fatal. A token that was minted and not recorded is better than a
        mint that fails because of its own bookkeeping -- the caller has the secret either
        way, and a revocation that failed to record must still have revoked."""
        who = actor()
        try:
            await self._audit.record(
                who.subject,
                who.via,
                "admin",
                action,
                resource=token_id,
                detail=detail,
                token_id=who.token_id,
            )
        except PyMongoError:
            logger.exception("could not record a token change in the audit history")

    async def ensure_indexes(self) -> None:
        """The digest is looked up on every request carrying a bearer token, and the owner on
        every listing."""
        await self._collection.create_index("token_digest", unique=True)
        await self._collection.create_index("owner")
