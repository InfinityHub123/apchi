"""Who is acting, carried to wherever the change actually lands.

The audit is written at chokepoints rather than in each of the forty-odd mutating routes
(#137), which means the code that records a change is several calls below the code that knows
who asked for it. A `contextvar` closes that gap -- the same mechanism `app.logging`'s
`apply_id_var` already uses to carry an Apply's id into every record, for the same reason.

The alternative is threading an actor through every Section function so that only the last
call uses it, and a parameter somebody can forget to pass is the per-route scheme again
wearing a different hat.
"""

from contextvars import ContextVar

from app.audit.events import SYSTEM
from app.auth.identity import Identity

#: Set by the gate that already resolves the caller, for the duration of one request.
_actor: ContextVar[Identity | None] = ContextVar("audit_actor", default=None)

#: What a change is attributed to when nothing was on the other end of a request: startup
#: recovery, an Auto Rollback, a Reset that a failed Apply performed. The role is `admin`
#: because Apchi acting on its own behalf is not restricted by its own gates -- the field is
#: required and nothing reads it here, and an `operator` would read as a claim that it was.
SYSTEM_ACTOR = Identity(subject=SYSTEM, role="admin", via="none")


def acting_as(identity: Identity) -> None:
    """Record who this request is for. Called once, by the gate every mutation passes."""
    _actor.set(identity)


def actor() -> Identity:
    """The caller, or Apchi itself.

    Never None, and the fallback is a *named* actor rather than a null one: a record reading
    "apchi reset this" is answerable, where one that lost its actor looks like a bug in the
    audit rather than a change nobody made.
    """
    return _actor.get() or SYSTEM_ACTOR
