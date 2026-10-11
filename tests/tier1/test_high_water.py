"""The high-water mark: Apchi knowing it has lost Snapshots.

§18's disaster is that MongoDB holds the only copy of every Snapshot. The first thing wrong
with it is not the loss, it is that Apchi could not tell: against an empty database it
reported itself healthy, `/snapshots` returned `[]` and `/candidate` returned 404, and none
of that distinguishes a fresh install from a catastrophic loss -- while the safe action in
each case is the opposite one.

The irreversible consequence was in Adoption, which refuses only when Snapshots exist. After
a loss there are none, so the obvious thing an Operator would try ran happily, minted a new
Snapshot 1, and destroyed the evidence that a backup was worth restoring.
"""

from httpx import AsyncClient

from app.config import Settings
from app.pipeline.history import CLUSTER_KEY, HIGHEST_KEY, HighWaterMark
from tests.conftest import FakeKubernetes

BASE = "/api/v1/admin/adoption"


async def _mark(fake_kubernetes: FakeKubernetes, settings: Settings) -> dict[str, str]:
    return await fake_kubernetes.read_config_map(settings.high_water_config_map)


# --- what it records ----------------------------------------------------------------


async def test_committing_raises_the_mark(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Written on Commit, because that is the only place a Snapshot comes into being."""
    await applying_client.post("/api/v1/catalogs", json={"name": "a", "connector": "memory"})
    apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]
    await _settled(applying_client, apply_id)

    assert (await _mark(fake_kubernetes, settings))[HIGHEST_KEY] == "1"


async def test_the_mark_is_never_lowered(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Its whole value is remembering a Snapshot MongoDB has forgotten.

    A restore from an old backup legitimately leaves MongoDB behind, and a mark that
    followed it down would quietly agree that the newer Snapshots never existed -- which is
    the agreement this exists to refuse.
    """
    mark = HighWaterMark(fake_kubernetes, settings)
    await mark.record(13)

    await mark.record(7)

    recorded = await mark.read()
    assert recorded is not None
    assert recorded.highest_snapshot == 13


async def test_the_cluster_id_is_minted_once_and_kept(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Identity for #114 to check a backup against, so one Cluster's credentials cannot be
    restored into another."""
    mark = HighWaterMark(fake_kubernetes, settings)
    await mark.record(1)
    first = (await _mark(fake_kubernetes, settings))[CLUSTER_KEY]

    await mark.record(2)

    assert (await _mark(fake_kubernetes, settings))[CLUSTER_KEY] == first
    assert first


# --- what it concludes --------------------------------------------------------------


async def test_no_mark_is_a_new_install_not_a_disaster(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Three situations look like this and none of them is a loss: a first run, an Apchi
    that predates the mark, and a mark somebody deleted. Claiming a disaster without
    evidence is worse than staying quiet."""
    history = await HighWaterMark(fake_kubernetes, settings).history(None)

    assert history.intact


async def test_a_mark_ahead_of_mongo_is_a_loss(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    await HighWaterMark(fake_kubernetes, settings).record(13)

    history = await HighWaterMark(fake_kubernetes, settings).history(None)

    assert history.lost
    assert history.ever == 13
    assert "committed 13" in str(history)


async def test_a_mark_mongo_has_caught_up_with_is_intact(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    await HighWaterMark(fake_kubernetes, settings).record(13)

    assert (await HighWaterMark(fake_kubernetes, settings).history(13)).intact


async def test_an_unreadable_mark_does_not_accuse_apchi_of_a_loss(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Apchi tolerates an unreachable Kubernetes by design. A transient 403 or a refused
    connection must not be reported as the loss of every Snapshot."""

    async def refuse(name: str) -> dict[str, str]:
        raise RuntimeError("the API server is not answering")

    fake_kubernetes.read_config_map = refuse  # type: ignore[method-assign]

    assert (await HighWaterMark(fake_kubernetes, settings).history(None)).intact


async def test_a_hand_edited_mark_is_reported_and_not_fatal(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Somebody will edit it. Refusing to parse it is right; crashing on it is not."""
    fake_kubernetes.config_maps[settings.high_water_config_map] = {HIGHEST_KEY: "thirteen"}

    assert await HighWaterMark(fake_kubernetes, settings).read() is None


# --- what it refuses ----------------------------------------------------------------


async def test_health_says_the_history_is_lost(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """And does not 503 doing it. This endpoint is the chart's readiness *and* liveness
    probe, so a 503 is how Kubernetes is told to kill the pod -- right while MongoDB is
    unreachable, and exactly wrong after a loss, because restoring a backup means talking
    to an Apchi that is up."""
    from httpx import ASGITransport

    from app.main import create_app

    await HighWaterMark(fake_kubernetes, settings).record(13)
    app = create_app(settings)
    # Substituted before the lifespan, which is how every test here gives Apchi a
    # Kubernetes -- so the answer below is the one Apchi's own startup worked out, not one
    # this test put there.
    app.state.kubernetes = fake_kubernetes
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        response = await client.get("/api/v1/health")

    assert response.status_code == 200, "a lost history must not crashloop the pod"
    assert response.json()["history"] == "lost"
    assert response.json()["snapshots_ever"] == 13


async def test_adopting_a_cluster_whose_history_was_lost_is_refused(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """The check that used to stand here -- "this Cluster already has Snapshots" -- passes
    after a loss, because there are none left to refuse on."""
    await HighWaterMark(fake_kubernetes, settings).record(13)
    await _ready_to_adopt(applying_client)

    refused = await applying_client.post(f"{BASE}/adopt")

    assert refused.status_code == 409
    message = refused.json()["message"]
    assert "committed Snapshot 13" in message
    assert "discard_history_through=13" in message, (
        "the refusal has to say exactly how to proceed, or the only way through is guesswork"
    )


async def test_the_override_requires_the_number_apchi_named(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """A number rather than a flag, so it cannot be set out of habit and cannot be copied
    from somebody else's runbook into a Cluster it does not describe."""
    await HighWaterMark(fake_kubernetes, settings).record(13)
    await _ready_to_adopt(applying_client)

    wrong = await applying_client.post(f"{BASE}/adopt?discard_history_through=12")

    assert wrong.status_code == 409


async def test_the_override_lets_a_deliberate_restart_through(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Someone whose history is genuinely gone has to be able to start again from what the
    Cluster is running. Refusing forever would make §18 unrecoverable rather than careful."""
    await HighWaterMark(fake_kubernetes, settings).record(13)
    await _ready_to_adopt(applying_client)

    started = await applying_client.post(f"{BASE}/adopt?discard_history_through=13")

    assert started.status_code == 202, started.json()


async def test_an_ordinary_adoption_is_untouched(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """No mark and no Snapshots is the ordinary first onboarding, and it must not be
    reported as a disaster or need an override."""
    await _ready_to_adopt(applying_client)

    started = await applying_client.post(f"{BASE}/adopt")

    assert started.status_code == 202, started.json()


# --- helpers ------------------------------------------------------------------------


async def _settled(client: AsyncClient, apply_id: str) -> dict:
    import asyncio

    from app.pipeline.applies import TERMINAL

    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.02)
    raise AssertionError("never settled")


async def _ready_to_adopt(client: AsyncClient) -> None:
    created = await client.post(f"{BASE}/discoveries")
    assert created.status_code == 201, created.json()
    assert created.json()["complete"], created.json()["outstanding"]
    seeded = await client.post(f"{BASE}/seed")
    assert seeded.status_code == 200, seeded.json()
