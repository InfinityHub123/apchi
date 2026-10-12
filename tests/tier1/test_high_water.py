"""The high-water mark, and the Adoption precondition it exists for.

Adoption runs once, over a Cluster Apchi does not yet manage (§15), and it decides whether a
Cluster is already managed by asking whether MongoDB holds Snapshots. An empty `snapshots`
collection is two situations -- never managed, and managed by an Apchi pointed at an empty
database -- and Adoption's answer differs: configure the first, refuse the second. Adopting
the second mints Snapshot 1 over a Cluster that already reached thirteen, and that is not
undoable.

So Apchi records the highest Snapshot number it has ever committed in a ConfigMap, where an
empty database cannot deny it. Keeping MongoDB is the platform's job, not Apchi's, so this is
not a backup or a recovery mechanism: it answers one question for one route.
"""

from httpx import AsyncClient

from app.config import Settings
from app.pipeline.high_water import HIGHEST_KEY, HighWaterMark
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
    """Lowering it would let a Cluster pointed at an older copy of its own database look
    like one that never went further -- the single thing the mark is here to prevent."""
    mark = HighWaterMark(fake_kubernetes, settings)
    await mark.record(13)

    await mark.record(7)

    assert await mark.read() == 13


async def test_no_mark_reads_as_no_evidence(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Three situations look like this and none of them should block an Adoption: a first
    run, an Apchi that predates the mark, and a mark somebody deleted."""
    assert await HighWaterMark(fake_kubernetes, settings).read() is None


async def test_a_hand_edited_mark_is_reported_and_not_fatal(
    fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """Somebody will edit it. Refusing to parse it is right; crashing on it is not."""
    fake_kubernetes.config_maps[settings.high_water_config_map] = {HIGHEST_KEY: "thirteen"}

    assert await HighWaterMark(fake_kubernetes, settings).read() is None


# --- what it refuses ----------------------------------------------------------------


async def test_adopting_a_cluster_that_has_committed_snapshots_is_refused(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """The check that used to stand alone -- "this Cluster already has Snapshots" -- asks
    MongoDB, and passes when MongoDB is empty whatever the reason."""
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
    """Someone whose Snapshots are genuinely gone has to be able to start again from what
    the Cluster is running. Refusing forever would be a trap rather than a precaution."""
    await HighWaterMark(fake_kubernetes, settings).record(13)
    await _ready_to_adopt(applying_client)

    started = await applying_client.post(f"{BASE}/adopt?discard_history_through=13")

    assert started.status_code == 202, started.json()


async def test_an_ordinary_adoption_is_untouched(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """No mark and no Snapshots is the ordinary first onboarding, and it must not need an
    override."""
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
