"""Password hashing, and the one trap in verifying one.

**argon2-cffi**, Argon2id, at the library's own defaults. Not passlib, which most FastAPI
material still recommends: it has been unmaintained since 2020 and is built on the `crypt`
module Python 3.13 removed.

Defaults rather than tuned parameters, deliberately. argon2-cffi's are chosen against the
OWASP guidance and revised with the library; a number picked here would be a number nobody
revisits. `needs_rehash` is what makes raising them later a login rather than a migration.
"""

import logging
import secrets
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@cache
def _unmatchable() -> str:
    """A real hash of a value nobody holds, verified against when there is no user.

    This is what makes an unknown username cost the same as a wrong password. Without it the
    response time says which usernames exist, which is a user enumeration oracle on a
    control plane whose users are named platform staff.

    Computed on first use rather than at import, so a Cluster in `none` mode never pays for
    it, and cached so it is not an easy way to make Apchi hash on demand.
    """
    return _hasher.hash(secrets.token_urlsafe(32))


def verify(password: str, stored: str | None) -> bool:
    """Whether `password` matches. `stored` of None means no such user -- and is still paid for.

    Returns False for every kind of failure. The caller must not distinguish them either:
    "wrong password", "no such user" and "disabled" are one answer to whoever is asking.
    """
    try:
        _hasher.verify(stored if stored is not None else _unmatchable(), password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        # A stored value that is not an Argon2 hash at all: a hand-edited document, or the
        # initial-admin setting given a plaintext password by mistake. Refused rather than
        # compared some other way, and logged because an Admin cannot otherwise tell this
        # apart from forgetting their own password.
        logger.error("a stored password hash is not readable; that user cannot log in")
        return False
    return stored is not None


def needs_rehash(stored: str) -> bool:
    """Whether this hash was made with weaker parameters than today's.

    Checked on a successful login, so raising the cost later re-hashes each user as they
    arrive instead of needing a migration nobody runs.
    """
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return False
