"""Admin changes, which leave no other trace anywhere.

§14 invariant 9 keeps Admin values out of Snapshots deliberately, and §12 makes the Snapshot
the record of what was committed. So for this one class of change -- the class that can alter
the Cluster without appearing in any Snapshot -- the audit is not a second record. It is the
only one.

The route that matters most is `PUT /admin/permissions/enforcement`. It can open a Cluster to
every identity without a grant, which is the reason that surface is Admin-only at all, and
until this slice nothing anywhere recorded who used it.

The other half of the file is the line the audit must not cross. §20 wants to know *which*
Admin value changed; it must not become the place Admin values themselves are kept, because
some of them are content an Admin injected and §14 went to the trouble of keeping them out of
Snapshots.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.audit.events import AuditEvent
from app.auth.identity import Identity
from app.config import AuthMode, Settings
from app.main import create_app
from app.pipeline.maintenance import EngagedBy
from tests.conftest import FakeKubernetes

AUDIT = "/api/v1/audit"
ENFORCEMENT = "/api/v1/admin/permissions/enforcement"
MAINTENANCE = "/api/v1/admin/maintenance-mode"

ADMIN = Identity(subject="root", role="admin", via="password")
OPERATOR = Identity(subject="jo", role="operator", via="password")


async def _events(client: AsyncClient, category: str = "admin") -> list[AuditEvent]:
    page = (await client.get(AUDIT, params={"limit": 200, "category": category})).json()
    return [AuditEvent.model_validate(event) for event in page["events"]]


async def _serving(
    settings: Settings, fake: FakeKubernetes, who: Identity
) -> AsyncIterator[AsyncClient]:
    # `password` mode, because minting a token is refused outright in `none` mode -- there is
    # nobody to own one when every caller is anonymous (#131). The caller is still
    # substituted; what the mode decides here is only whether a token may exist.
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    app.dependency_overrides[caller] = lambda: who
    app.state.kubernetes = fake
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
async def as_admin(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    async for http in _serving(settings, fake_kubernetes, ADMIN):
        yield http


@pytest.fixture
async def as_operator(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    async for http in _serving(settings, fake_kubernetes, OPERATOR):
        yield http


# --- the switch that can open a Cluster ---------------------------------------------------


async def test_enforcement_is_audited_in_both_directions(as_admin: AsyncClient) -> None:
    """Turning it off is the dangerous direction and the one somebody will ask about, so the
    event says which way it went. A boolean policy flag carries no credential, which is why
    this is the one Admin value whose content reaches the trail."""
    on = await as_admin.put(ENFORCEMENT, json={"enforced": True})
    assert on.status_code == 200, on.text
    off = await as_admin.put(ENFORCEMENT, json={"enforced": False})
    assert off.status_code == 200, off.text

    events = [e for e in await _events(as_admin) if e.resource == "enforce_permissions"]

    assert [e.detail for e in events] == [
        "enforce_permissions set to false",
        "enforce_permissions set to true",
    ]
    assert {e.actor for e in events} == {"root"}
    assert {e.category for e in events} == {"admin"}
    assert {e.snapshot for e in events} == {None}, "an Admin change creates no Snapshot (§14)"


async def test_an_admin_change_carries_no_snapshot(as_admin: AsyncClient) -> None:
    """The invariant, asserted rather than assumed: §14 gives an Admin Apply no Snapshot, so
    a number here would claim a commit that never happened."""
    await as_admin.put(ENFORCEMENT, json={"enforced": True})

    assert [e.snapshot for e in await _events(as_admin)] == [None]


# --- maintenance mode, both ways ----------------------------------------------------------


async def test_engaging_and_releasing_maintenance_mode_are_distinguishable(
    as_admin: AsyncClient,
) -> None:
    """Engaging it freezes every Operator's work; releasing it after a failed Auto Rollback is
    a human saying the Cluster's state is understood again. Both are claims worth
    attributing, and they are not the same claim."""
    engaged = await as_admin.put(
        MAINTENANCE, json={"engaged": True, "reason": "a platform upgrade"}
    )
    assert engaged.status_code == 200, engaged.text
    released = await as_admin.put(MAINTENANCE, json={"engaged": False})
    assert released.status_code == 200, released.text

    actions = [e.action for e in await _events(as_admin) if e.resource == "maintenance_mode"]

    assert actions == ["released", "engaged"]


async def test_maintenance_mode_engaged_by_apchi_is_attributed_to_apchi(
    as_admin: AsyncClient,
) -> None:
    """The case somebody arriving at a frozen Cluster most needs told apart from an Admin
    freezing it for an upgrade: §14 engages it itself when a failed Auto Rollback leaves the
    Cluster in a state nobody has described."""
    app = as_admin._transport.app  # type: ignore[attr-defined]

    await app.state.maintenance_store.engage(
        "auto rollback failed", EngagedBy.AUTO_ROLLBACK_FAILURE
    )

    [event] = [e for e in await _events(as_admin) if e.resource == "maintenance_mode"]
    assert event.actor == "apchi"
    assert event.via == "none"
    assert event.detail is not None and "auto_rollback_failure" in event.detail


# --- refusals, which matter more here than anywhere -------------------------------------


async def test_an_operator_refused_the_admin_surface_is_recorded(
    as_operator: AsyncClient,
) -> None:
    """For an Operator edit a refusal is usually a freeze. Here it is somebody reaching for
    the switch that disables permission enforcement and being told no, which is a different
    kind of question and the reason the actor is recorded before the role is checked."""
    refused = await as_operator.put(ENFORCEMENT, json={"enforced": False})

    assert refused.status_code == 403, refused.text
    [event] = await _events(as_operator)
    assert event.actor == "jo"
    assert event.outcome == "refused"
    assert event.action == "attempted"
    assert event.category == "admin"


# --- tokens -------------------------------------------------------------------------------


async def test_minting_and_revoking_a_token_are_audited(as_admin: AsyncClient) -> None:
    """Not a configuration change, but it creates a credential that acts as its owner with
    their current role."""
    minted = await as_admin.post("/api/v1/tokens", json={"name": "nightly"})
    assert minted.status_code == 201, minted.text
    token_id = minted.json()["token"]["id"]

    revoked = await as_admin.delete(f"/api/v1/tokens/{token_id}")
    assert revoked.status_code == 204, revoked.text

    events = [e for e in await _events(as_admin) if e.resource == token_id]
    assert [e.action for e in events] == ["revoked", "minted"]
    assert {e.actor for e in events} == {"root"}


async def test_no_token_secret_reaches_the_audit(as_admin: AsyncClient) -> None:
    """The digest is all Apchi keeps of a token (#131). The audit must not become the one
    place the secret itself appears."""
    minted = await as_admin.post("/api/v1/tokens", json={"name": "nightly"})
    secret = minted.json()["secret"]

    body = (await as_admin.get(AUDIT, params={"limit": 200})).text

    assert secret not in body
    assert secret.split("apchi_")[1][:16] not in body


# --- the line the audit must not cross ----------------------------------------------------


async def test_admin_values_themselves_never_reach_an_event(as_admin: AsyncClient) -> None:
    """Only which value changed. §14 invariant 9 keeps Admin values out of Snapshots on
    purpose, and an audit that recorded their content would be the back door that undoes
    that -- a second copy, with its own access rules and its own export path."""
    pattern = "CN=secret-internal-hostname,OU=Platform"
    response = await as_admin.put(
        "/api/v1/admin/certificate-mapping/preserved",
        json={"patterns": [{"pattern": pattern, "user": "$1"}]},
    )
    assert response.status_code in (200, 422), response.text

    body = (await as_admin.get(AUDIT, params={"limit": 200})).text

    assert pattern not in body, "an Admin value's content reached the audit trail"
    if response.status_code == 200:
        resources = [e.resource for e in await _events(as_admin)]
        assert "preserved_certificate_mappings" in resources, resources


async def test_only_the_values_that_changed_are_recorded(as_admin: AsyncClient) -> None:
    """A replace that leaves a value alone is not a change to it. Without this every Admin
    write would record all three, and the trail would stop answering what was touched."""
    await as_admin.put(ENFORCEMENT, json={"enforced": True})
    before = len(await _events(as_admin))

    again = await as_admin.put(ENFORCEMENT, json={"enforced": True})

    assert again.status_code == 200, again.text
    assert len(await _events(as_admin)) == before, "an unchanged value was recorded as a change"
