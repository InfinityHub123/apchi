"""Shared request dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.api.errors import Conflict, Forbidden, MaintenanceModeEngaged, Unauthenticated
from app.auth.identity import NOBODY, Identity
from app.auth.sessions import COOKIE, SessionStore
from app.auth.tokens import PREFIX, TokenStore
from app.auth.users import UserStore
from app.config import AuthMode, Settings
from app.pipeline.admin_values import AdminStore
from app.pipeline.applies import ApplyStore
from app.pipeline.candidate import CandidateStore
from app.pipeline.discoveries import DiscoveryStore
from app.pipeline.maintenance import MaintenanceStore
from app.pipeline.snapshots import SnapshotStore
from app.pipeline.validations import ValidationStore
from app.sections.base import Cluster


def trino(request: Request) -> Trino:
    store: Trino = request.app.state.trino
    return store


TrinoDep = Annotated[Trino, Depends(trino)]


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(settings_of)]


def admin_store(request: Request) -> AdminStore:
    store: AdminStore = request.app.state.admin_store
    return store


AdminStoreDep = Annotated[AdminStore, Depends(admin_store)]


async def cluster(request: Request) -> Cluster:
    """The Cluster as a route sees it, with the Admin values loaded per request.

    Read every time rather than cached: Review's job is to report what applying *now* would
    cost, and an Admin change between two Reviews is exactly the thing that must show up.
    """
    return Cluster(
        trino=request.app.state.trino,
        kubernetes=request.app.state.kubernetes,
        settings=request.app.state.settings,
        admin=await admin_store(request).load(),
    )


ClusterDep = Annotated[Cluster, Depends(cluster)]


def candidate_store(request: Request) -> CandidateStore:
    store: CandidateStore = request.app.state.candidate_store
    return store


CandidateStoreDep = Annotated[CandidateStore, Depends(candidate_store)]


def snapshot_store(request: Request) -> SnapshotStore:
    store: SnapshotStore = request.app.state.snapshot_store
    return store


SnapshotStoreDep = Annotated[SnapshotStore, Depends(snapshot_store)]


def apply_store(request: Request) -> ApplyStore:
    store: ApplyStore = request.app.state.apply_store
    return store


ApplyStoreDep = Annotated[ApplyStore, Depends(apply_store)]


def validation_store(request: Request) -> ValidationStore:
    store: ValidationStore = request.app.state.validation_store
    return store


ValidationStoreDep = Annotated[ValidationStore, Depends(validation_store)]


def maintenance_store(request: Request) -> MaintenanceStore:
    store: MaintenanceStore = request.app.state.maintenance_store
    return store


MaintenanceStoreDep = Annotated[MaintenanceStore, Depends(maintenance_store)]


async def caller(request: Request) -> Identity:
    """Who is making this request.

    One resolver for every mode, so a route never asks which mode is configured. In `none`
    mode that is the anonymous Admin (`NOBODY`), which is what keeps this slice a seam rather
    than a behaviour change -- every route gains a gate and every caller still passes it.

    There is deliberately **no fallback**. A mode that cannot establish an identity raises;
    it never degrades to `none`. A control plane that silently drops its own authentication
    because a credential store is unreachable is worse than one that refuses.
    """
    settings: Settings = request.app.state.settings
    if settings.auth_mode is AuthMode.NONE:
        return NOBODY

    # A bearer token first: it is how automations call, and they never carry a session.
    if (identity := await _from_bearer(request)) is not None:
        return identity

    if settings.auth_mode is AuthMode.PASSWORD:
        if (identity := await _from_session(request)) is not None:
            return identity
        raise Unauthenticated(
            "Sign in at POST /api/v1/auth/login, or send a token as "
            "'Authorization: Bearer apchi_...'."
        )

    # sso (#132) adds its branch here. A configured mode with no backend raises rather than
    # resolving to an Admin: a control plane that drops its own authentication because a
    # provider is unreachable is worse than one that refuses.
    raise Unauthenticated(
        f"Authentication mode {settings.auth_mode.value!r} is configured but this build has "
        "no way to authenticate against it."
    )


async def _from_bearer(request: Request) -> Identity | None:
    """An `Authorization: Bearer apchi_...` header, resolved to the token's **owner**.

    The role is the owner's current one, read on every request. That is the whole point of an
    opaque token: demoting somebody demotes their automations, and disabling somebody stops
    them, without anybody remembering to go and find their tokens.

    Returns None for anything that is not one of Apchi's tokens, so the caller falls through
    to the mode's own method rather than being told a session cookie is a bad token.
    """
    header = request.headers.get("authorization", "")
    scheme, _, secret = header.partition(" ")
    if scheme.lower() != "bearer" or not secret.startswith(PREFIX):
        return None

    tokens: TokenStore = request.app.state.token_store
    token = await tokens.find(secret)
    if token is None or not token.live:
        raise Unauthenticated("That token is not valid, has expired, or has been revoked.")

    users: UserStore = request.app.state.user_store
    owner = await users.get(token.owner)
    if owner is None or owner.disabled:
        raise Unauthenticated(
            f"The account that owns this token ({token.owner!r}) is disabled or gone."
        )

    await tokens.touch(token.id)
    return Identity(subject=owner.username, role=owner.role, via="token", token_id=token.id)


async def _from_session(request: Request) -> Identity | None:
    """The caller's session cookie, resolved against the sessions collection and the user.

    Two reads, and the second one is the point: the role comes from the **user**, not from
    the session row, and a user who has been disabled or removed since signing in stops
    working on their next request rather than when their session would have expired. Storing
    the role on the session would have made "disable this person now" depend on also deleting
    their sessions -- correct only as long as nobody forgets, which is not a property worth
    resting on.

    It also means sessions and tokens (#131) answer to the same rule, so there is one
    freshness story rather than two.
    """
    value = request.cookies.get(COOKIE)
    if not value:
        return None
    session = await request.app.state.session_store.read(value)
    if session is None:
        return None
    users: UserStore = request.app.state.user_store
    user = await users.get(session.username)
    if user is None or user.disabled:
        return None
    return user.identity()


CallerDep = Annotated[Identity, Depends(caller)]


async def authenticated(identity: CallerDep) -> Identity:
    """An identity, or 401. Every /api/v1 route carries this except /health."""
    return identity


Authenticated = Depends(authenticated)


async def admin_only(identity: CallerDep) -> Identity:
    """An Admin, or 403.

    Built on `caller` rather than beside `Authenticated`, so it *implies* authentication
    rather than offering an alternative to it: two independent dependencies where one implies
    the other is how a route ends up carrying only the weaker one.
    """
    if not identity.is_admin:
        raise Forbidden(
            f"This is an Admin operation and {identity.subject!r} is an Operator. Admins "
            "inject low-level configuration and freeze mutation; Operators own the six "
            "Sections (§3)."
        )
    return identity


AdminOnly = Depends(admin_only)


async def mutations_enabled(maintenance: MaintenanceStoreDep) -> None:
    """Rejects a mutation while Maintenance Mode is engaged.

    Either an Admin disabled changes for a platform upgrade, or Apchi did because the
    Cluster's state is unknown after a failed Auto Rollback. Reads are unaffected, and
    so are Admin operations -- releasing the mode has to work while it is engaged.
    """
    state = await maintenance.get()
    if state.engaged:
        raise MaintenanceModeEngaged(
            "Changes are temporarily disabled. "
            + (state.reason or "An Admin has engaged Maintenance Mode.")
        )


MutationsEnabled = Depends(mutations_enabled)


async def operator_mutation_allowed(
    identity: CallerDep, applies: ApplyStoreDep, maintenance: MaintenanceStoreDep
) -> None:
    """The one gate every Operator edit passes.

    Three reasons an edit is refused, and they are not the same thing. Not being anybody is
    the first: an unauthenticated caller is refused before Apchi looks at whether changes are
    allowed at all. Maintenance Mode is the broader of the other two and is checked next. The
    freeze is narrower and temporary: the Candidate is frozen for the whole of an Apply so
    that what is committed is what was verified.

    All of it is one dependency rather than three, so a new mutating route cannot pick up
    part of the gate -- which is why the identity belongs in here rather than beside it.
    """
    del identity  # resolving it is the check; its value is the audit's concern (§20).
    await mutations_enabled(maintenance)

    running = await applies.in_flight()
    if running is not None:
        raise Conflict(
            f"The Configuration Candidate is frozen: Apply {running.id} is in flight "
            f"({running.stage.value}). Changes are rejected until it finishes."
        )


OperatorMutationAllowed = Depends(operator_mutation_allowed)


def user_store(request: Request) -> UserStore:
    store: UserStore = request.app.state.user_store
    return store


UserStoreDep = Annotated[UserStore, Depends(user_store)]


def token_store(request: Request) -> TokenStore:
    store: TokenStore = request.app.state.token_store
    return store


TokenStoreDep = Annotated[TokenStore, Depends(token_store)]


def session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


SessionStoreDep = Annotated[SessionStore, Depends(session_store)]


def discovery_store(request: Request) -> DiscoveryStore:
    store: DiscoveryStore = request.app.state.discovery_store
    return store


DiscoveryStoreDep = Annotated[DiscoveryStore, Depends(discovery_store)]


def kubernetes(request: Request) -> KubernetesAdapter:
    adapter: KubernetesAdapter = request.app.state.kubernetes
    return adapter


KubernetesDep = Annotated[KubernetesAdapter, Depends(kubernetes)]
