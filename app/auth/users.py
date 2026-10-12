"""The people who may call Apchi, when a password is how they prove it.

Provisioned by an Admin. There is no self-registration, no email verification and no
password-reset flow, because §3's Operators are platform staff somebody onboards rather than
members of the public -- and those three flows are most of what a batteries-included auth
library would bring.

A user is **disabled** rather than deleted. §20's audit records an actor, and an actor that no
longer resolves is worse than one marked gone: the trail would name somebody nothing can
explain.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.auth.identity import Identity, Role
from app.auth.passwords import hash_password, needs_rehash, verify

logger = logging.getLogger(__name__)


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password_hash: str
    role: Role
    disabled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def identity(self) -> Identity:
        return Identity(subject=self.username, role=self.role, via="password")


class UserStore:
    """Users, by username. The username is the `_id`, so uniqueness is the database's job
    rather than a check that races."""

    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["users"]

    async def get(self, username: str) -> User | None:
        document = await self._collection.find_one({"_id": username})
        if document is None:
            return None
        document.pop("_id", None)
        return User.model_validate(document)

    async def create(self, user: User) -> None:
        """Raises DuplicateKeyError when the username is taken."""
        await self._collection.insert_one({"_id": user.username, **user.model_dump(mode="json")})

    async def count(self) -> int:
        return await self._collection.count_documents({})

    async def _restore_hash(self, username: str, password_hash: str) -> None:
        await self._collection.update_one(
            {"_id": username}, {"$set": {"password_hash": password_hash}}
        )

    async def authenticate(self, username: str, password: str) -> User | None:
        """The user, or None -- and None never says which of the three reasons it was.

        "No such user", "wrong password" and "disabled" are one answer to whoever is asking.
        Each of them also pays a full Argon2 verification: an unknown username that returned
        early would let anyone enumerate Apchi's users by response time, and its users are
        named platform staff.
        """
        user = await self.get(username)
        matched = verify(password, None if user is None else user.password_hash)
        if user is None or not matched or user.disabled:
            logger.info("login refused", extra={"username": username})
            return None

        # Raising the hashing cost later then re-hashes each user as they arrive, instead of
        # needing a migration nobody runs.
        if needs_rehash(user.password_hash):
            await self._restore_hash(user.username, hash_password(password))
            logger.info("rehashed a password at stronger parameters", extra={"username": username})
        return user

    async def ensure_initial_admin(self, username: str, password_hash: str) -> bool:
        """Create the one bootstrap Admin, but only on a Cluster with no users at all.

        Returns whether it created one. The emptiness check is what makes this safe to leave
        in the chart forever: otherwise every restart would resurrect an Admin somebody
        deleted, or quietly reset a password somebody changed, and the setting would become a
        back door that looks like configuration.

        A hash rather than a password, so the plaintext is not in values, a release, or the
        output of `helm get values`.
        """
        if await self.count() > 0:
            return False
        try:
            await self.create(User(username=username, password_hash=password_hash, role="admin"))
        except DuplicateKeyError:
            # Two Apchis starting at once. The other one won, which is the right outcome.
            return False
        logger.warning("created the initial admin", extra={"username": username})
        return True

    async def ensure_indexes(self) -> None:
        """Nothing to add: the username is the `_id`, which is indexed and unique already.

        Here so the lifespan has one call per store rather than a special case, and so the
        reason there is no index is recorded where someone would look for one.
        """
        return None


async def bootstrap(store: UserStore, username: str | None, password_hash: str | None) -> None:
    """Create the initial Admin if the settings name one. Never fatal.

    A MongoDB that cannot be reached must not stop Apchi starting -- `/health` already
    reports 503 while it is down, which is the signal Kubernetes acts on, and a crashloop
    loses the logs that would explain it.
    """
    if not username or not password_hash:
        return
    try:
        await store.ensure_initial_admin(username, password_hash)
    except PyMongoError:
        logger.exception("could not create the initial admin; retried at the next start")
