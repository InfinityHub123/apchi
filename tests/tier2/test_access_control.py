"""The generated access-control file, mounted in a real cluster.

Tier 1 proves the file says the right thing. Only a real Trino reading a real mount
proves it does anything: the `owner` access mode this depends on is not in Trino's
documented list of allow values, and the whole restriction rests on it.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline.access_control import RULES_KEY, render_rules
from app.pipeline.preconditions import check, pod_spec
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

ACCESS_CONTROL_SECRET = "trino-access-control"


async def test_the_mounted_rules_are_what_apchi_generates(
    real_kubernetes: RealKubernetes,
) -> None:
    """The committed manifest is generated output, so the cluster and the generator must
    agree. CI checks the file; this checks the cluster."""
    mounted = await real_kubernetes.read_secret(ACCESS_CONTROL_SECRET)

    assert mounted[RULES_KEY] == render_rules(Settings().trino_user)


async def test_a_non_apchi_identity_cannot_create_a_catalog(forward: PortForward) -> None:
    """The restriction itself. `owner` is what checkCanCreateCatalog gates on."""
    other = Trino(host="127.0.0.1", port=forward.port, user="notapchi")

    with pytest.raises(Exception) as raised:
        await other.create_catalog(f"forbidden_{uuid.uuid4().hex[:6]}", "memory", {})

    assert "Access Denied" in str(raised.value)


async def test_apchis_identity_can_create_and_drop_a_catalog(forward: PortForward) -> None:
    """The other half: the rule has to grant as well as deny, and an anchored pattern
    must still match the identity it names."""
    apchi = Trino(host="127.0.0.1", port=forward.port, user=Settings().trino_user)
    name = f"allowed_{uuid.uuid4().hex[:6]}"

    await apchi.create_catalog(name, "memory", {})
    try:
        assert name in await apchi.catalogs()
    finally:
        await apchi.drop_catalog(name)


async def test_a_non_apchi_identity_keeps_full_access_to_existing_catalogs(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """`all` is full access without `owner`. Restricting DDL must not cost End Users the
    ability to use the catalogs Apchi creates for them."""
    name = f"shared_{uuid.uuid4().hex[:6]}"
    apchi = Trino(host="127.0.0.1", port=forward.port, user=Settings().trino_user)
    await apchi.create_catalog(name, "memory", {})
    try:
        other = Trino(host="127.0.0.1", port=forward.port, user="notapchi")

        assert name in await other.catalogs()
        await other.query(f'CREATE SCHEMA "{name}".probe')
        assert await other.query(f'SHOW SCHEMAS FROM "{name}"')
    finally:
        await apchi.drop_catalog(name)


async def test_the_real_deployment_meets_every_precondition(
    real_kubernetes: RealKubernetes,
) -> None:
    """Tier 1 checks the preconditions against a spec written by hand. This checks them
    against the manifests actually deployed, which is what makes the fixture honest."""
    settings = Settings()

    spec = pod_spec(await real_kubernetes.deployment_pod_spec(settings.coordinator_deployment_name))

    check(spec, settings)
