"""A raw file reaching the Cluster, and being taken back when the Cluster rejects it (§14).

The storage slice proved an Admin can write a path. This proves the three things that make
"if it works it works" a safe promise rather than a reckless one:

* it **arrives** -- the Secret is written and the file is mounted
* it **forces a restart**, because raw values are read once at start, and on an Admin Apply
  nothing an Operator owns has moved so no Section's plan can ask for one
* it **goes back** when the Apply fails, from the pre-image the storage slice keeps -- there
  is no Snapshot to restore Admin values from, which is the whole reason that pre-image exists

The last one is where an Operator Apply and an Admin Apply deliberately differ. Invariant 2
says Admin values survive a rollback; but when the Apply that failed *was* the Admin change,
leaving them would redeliver exactly what is being rolled back.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.auth.identity import Identity
from app.config import Settings
from app.main import create_app
from app.pipeline.engine import AdminEngine, Engine
from app.pipeline.raw_files import secret_key
from tests.conftest import FakeKubernetes

FILES = "/api/v1/admin/files"
ADMIN_APPLIES = "/api/v1/admin/applies"
EXTRA = "/etc/trino/extra.properties"
CONTENT = "query.max-memory=50GB\n"

ADMIN = Identity(subject="root", role="admin", via="password")


@pytest.fixture
async def as_admin(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    app.dependency_overrides[caller] = lambda: ADMIN
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


def _cluster_of(http: AsyncClient) -> object:
    app = http._transport.app  # type: ignore[attr-defined]
    return app.state


async def _write(http: AsyncClient, path: str = EXTRA, content: str = CONTENT) -> None:
    response = await http.put(f"{FILES}{path}", json={"content": content})
    assert response.status_code == 200, response.text


async def _deliver(http: AsyncClient) -> None:
    """Deliver through the engine's own path, with the Admin values as they stand."""
    from app.pipeline import raw_files
    from app.sections.base import Cluster

    state = _cluster_of(http)
    cluster = Cluster(
        trino=state.trino,
        kubernetes=state.kubernetes,
        settings=state.settings,
        admin=await state.admin_store.load(),
    )
    await raw_files.deliver(cluster)


# --- it arrives ---------------------------------------------------------------------------


async def test_a_raw_file_is_written_to_a_secret_and_mounted(
    as_admin: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _write(as_admin)

    await _deliver(as_admin)

    state = _cluster_of(as_admin)
    secret = await fake_kubernetes.read_secret(state.settings.raw_files_secret_name)
    assert secret == {secret_key(EXTRA): CONTENT}
    assert EXTRA in {mount.mount_path for mount in await _mounts(fake_kubernetes, state)}


async def test_two_files_with_one_basename_do_not_collide(
    as_admin: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Raw files sit at arbitrary paths, so two can share a filename. A Secret key derived
    from the name alone -- which is what a Section's `CoordinatorFile` uses -- would deliver
    one file to both places."""
    await _write(as_admin, "/etc/trino/a/x.properties", "first\n")
    await _write(as_admin, "/etc/trino/b/x.properties", "second\n")

    await _deliver(as_admin)

    state = _cluster_of(as_admin)
    secret = await fake_kubernetes.read_secret(state.settings.raw_files_secret_name)
    assert len(secret) == 2
    assert secret[secret_key("/etc/trino/a/x.properties")] == "first\n"
    assert secret[secret_key("/etc/trino/b/x.properties")] == "second\n"


async def test_removing_a_file_removes_its_mount(
    as_admin: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Removing the content is not enough. Trino refuses to start when a file it was told to
    read is missing, and a subPath mount of an absent Secret key becomes a *directory* Trino
    dies reading (§13.6) -- so "no longer wanted" has to mean no mount at all."""
    await _write(as_admin)
    await _deliver(as_admin)
    state = _cluster_of(as_admin)
    assert EXTRA in {m.mount_path for m in await _mounts(fake_kubernetes, state)}

    assert (await as_admin.delete(f"{FILES}{EXTRA}")).status_code == 204
    await _deliver(as_admin)

    assert EXTRA not in {m.mount_path for m in await _mounts(fake_kubernetes, state)}


# --- it forces a restart ------------------------------------------------------------------


async def test_a_new_raw_file_needs_a_rollout(as_admin: AsyncClient) -> None:
    """Raw values are read once at start -- that is what makes them the class §14 calls most
    able to stop Trino booting -- so any change needs a Rollout. On an Admin Apply every
    Section's plan is empty, which makes this the only thing that can ask for one."""
    from app.pipeline import raw_files
    from app.sections.base import Cluster

    await _write(as_admin)
    state = _cluster_of(as_admin)
    cluster = Cluster(
        trino=state.trino,
        kubernetes=state.kubernetes,
        settings=state.settings,
        admin=await state.admin_store.load(),
    )

    assert await raw_files.changed(cluster) is True


async def test_delivering_twice_needs_no_second_rollout(as_admin: AsyncClient) -> None:
    """Otherwise every Admin Apply on a Cluster with a raw file would terminate every running
    query for nothing -- the same argument `rollout_needed` already makes for Sections."""
    from app.pipeline import raw_files
    from app.sections.base import Cluster

    await _write(as_admin)
    await _deliver(as_admin)
    state = _cluster_of(as_admin)
    cluster = Cluster(
        trino=state.trino,
        kubernetes=state.kubernetes,
        settings=state.settings,
        admin=await state.admin_store.load(),
    )

    assert await raw_files.changed(cluster) is False


async def test_changing_the_content_needs_a_rollout(as_admin: AsyncClient) -> None:
    """The path is unchanged, so only the content comparison can notice."""
    from app.pipeline import raw_files
    from app.sections.base import Cluster

    await _write(as_admin)
    await _deliver(as_admin)
    await _write(as_admin, content="query.max-memory=1GB\n")

    state = _cluster_of(as_admin)
    cluster = Cluster(
        trino=state.trino,
        kubernetes=state.kubernetes,
        settings=state.settings,
        admin=await state.admin_store.load(),
    )

    assert await raw_files.changed(cluster) is True


# --- it goes back -------------------------------------------------------------------------


async def test_a_failed_admin_apply_restores_the_previous_values(as_admin: AsyncClient) -> None:
    """There is no Snapshot to restore Admin values from -- they never enter one (invariant
    9) -- so the pre-image the storage slice keeps is the only record of what the Cluster
    had."""
    state = _cluster_of(as_admin)
    await _write(as_admin, content="good\n")
    await _write(as_admin, content="bad\n")

    restored = await state.admin_store.restore_previous()

    assert restored.raw_files[EXTRA] == "good\n"
    assert (await state.admin_store.load()).raw_files[EXTRA] == "good\n"


async def test_restoring_twice_restores_the_same_thing(as_admin: AsyncClient) -> None:
    """§10's recovery can run a rollback after an Apchi restart as well as inside the Apply
    that failed, so restoring has to be idempotent. Rotating the pre-image here would make
    the *bad* values what a second rollback restores, which is the opposite of the word."""
    state = _cluster_of(as_admin)
    await _write(as_admin, content="good\n")
    await _write(as_admin, content="bad\n")

    first = await state.admin_store.restore_previous()
    second = await state.admin_store.restore_previous()

    assert first.raw_files == second.raw_files == {EXTRA: "good\n"}


async def test_an_operator_apply_does_not_revert_admin_values(
    as_admin: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate asymmetry, run rather than inspected.

    Invariant 2: Admin values survive a rollback, because they are platform state and an
    Operator's rollback should not revert them. Only an Admin Apply -- where the Admin change
    *is* what failed -- takes them back. Both engines are driven here, because a structural
    check that one overrides a method would pass even if the override did the wrong thing.
    """

    # The Section restore is stubbed: it issues catalog DDL against Trino, which this
    # fixture does not have, and what is under test is which engine takes the Admin values
    # back -- not how Sections are restored, which test_auto_rollback.py covers.
    async def no_sections(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.pipeline.engine.restore", no_sections)

    state = _cluster_of(as_admin)
    await _write(as_admin, content="good\n")
    await _write(as_admin, content="bad\n")

    await _engine(state, Engine).roll_back()

    assert (await state.admin_store.load()).raw_files[EXTRA] == "bad\n", (
        "an Operator Apply's rollback reverted Admin values, which invariant 2 says it must "
        "not: they are platform state and survive an Operator's rollback"
    )

    await _engine(state, AdminEngine).roll_back()

    assert (await state.admin_store.load()).raw_files[EXTRA] == "good\n", (
        "an Admin Apply's rollback left the values that broke the Cluster in place"
    )


def _engine(state: object, kind: type[Engine]) -> Engine:
    """The engine the app would build, with the same wiring."""
    return kind(
        apply_id="apl_test",
        settings=state.settings,  # type: ignore[attr-defined]
        candidates=state.candidate_store,  # type: ignore[attr-defined]
        snapshots=state.snapshot_store,  # type: ignore[attr-defined]
        trino=state.trino,  # type: ignore[attr-defined]
        kubernetes=state.kubernetes,  # type: ignore[attr-defined]
        maintenance=state.maintenance_store,  # type: ignore[attr-defined]
        admin=state.admin_store,  # type: ignore[attr-defined]
    )


async def _mounts(fake: FakeKubernetes, state: object) -> list[object]:
    from app.pipeline import preconditions

    spec = preconditions.pod_spec(
        await fake.deployment_pod_spec(state.settings.coordinator_deployment_name)  # type: ignore[attr-defined]
    )
    return [
        mount
        for container in spec.containers
        for mount in container.volume_mounts
        if mount.name == state.settings.raw_files_volume_name  # type: ignore[attr-defined]
    ]
