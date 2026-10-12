"""The caller, resolved to one of §3's two Apchi roles.

§3 has exactly two: Operators own all six Sections, and Admins do everything Operators can
plus inject low-level configuration and freeze mutation. End Users are never Apchi users --
they are the subject of permissions and resource groups and have no identity here at all, so
there is no role for them to hold.

Shaped for §20 as much as for §19. Audit records an actor, and `subject` plus `via` is what
makes "who changed this" answerable in a way that survives a token being revoked or a user
being disabled: the record says what authenticated, not just who claimed to.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: Apchi's own roles. Not Trino's -- a `TrinoIdentity` is an End User, which is the subject of
#: configuration rather than a caller of Apchi.
Role = Literal["operator", "admin"]

#: How the caller proved who they are. `none` is the mode where nobody proves anything.
Via = Literal["none", "password", "sso", "token"]

#: What a caller is called when the mode asks nobody to authenticate. A real name rather than
#: an empty string, so an audit record from such a Cluster reads as "anonymous did this"
#: instead of looking like a record that lost its actor.
ANONYMOUS = "anonymous"


class Identity(BaseModel):
    """One caller, for one request."""

    model_config = ConfigDict(frozen=True)

    subject: str
    role: Role
    via: Via
    #: Set when `via` is "token": which token, so a revocation has something to name and an
    #: audit trail distinguishes an automation from the person who minted it.
    token_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


#: The caller in `none` mode. An **Admin**, and the choice matters: every endpoint is open
#: today, so resolving an unauthenticated caller to an Operator would *remove* access to
#: /admin/* that exists now, breaking installs on upgrade in the name of adding security to
#: them. A Cluster that asks nobody to authenticate cannot distinguish the two roles anyway.
NOBODY = Identity(subject=ANONYMOUS, role="admin", via="none")
