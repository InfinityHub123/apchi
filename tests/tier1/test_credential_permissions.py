"""Credential files Apchi puts on a pod are not world-readable.

A Secret volume's files are written 0644 and owned by **root** unless the pod says
otherwise, and Trino runs as uid 1000. Those two facts between them are why this needs
tests rather than a constant: 0644 leaks a private key to every process in the pod and
libpq refuses to use it at all, while the obvious fix of 0600 produces a root-only file
Trino cannot read. 0640 plus an `fsGroup` is the one combination that is both restricted
and readable. See §13.2.

The real adapter is exercised here rather than the fake. `RealKubernetes` loads its client
lazily, so it can be built without a cluster and asked what patch it would send -- which is
the decision worth testing, and the fake can only mirror it. What the kubelet then does with
that patch is tier 2's, in `test_validation_pod.py`.
"""

from typing import Any

import pytest

from app.adapters.kubernetes import CREDENTIAL_FILE_MODE, RealKubernetes
from app.config import Settings
from app.pipeline.files import credential_secrets
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from app.pipeline.validation import validation_manifest
from app.sections.registry import REGISTERED
from tests.conftest import FakeKubernetes, healthy_pod_spec

TRINO_IMAGE = "trinodb/trino:483"


def test_the_mode_is_restricted_but_still_group_readable() -> None:
    """0600 would be root-only, because a Secret file is root-owned whatever the container
    runs as. Group-read is what Trino needs and what libpq still accepts."""
    assert CREDENTIAL_FILE_MODE == 0o640
    # No world bits, no group write, no group execute -- libpq's exact rule for a
    # root-owned key file.
    assert not CREDENTIAL_FILE_MODE & 0o037
    # And readable by the group, or Trino could not read its own keys.
    assert CREDENTIAL_FILE_MODE & 0o040


class _Patched(RealKubernetes):
    """A RealKubernetes that records its patch instead of sending it."""

    def __init__(self, fs_group: int | None) -> None:
        super().__init__(namespace="apchi")
        self._fs_group_value = fs_group
        self.patches: list[dict[str, Any]] = []

    async def deployment_pod_spec(self, deployment: str) -> dict[str, Any]:
        if self._fs_group_value is None:
            return {"containers": []}
        return {"containers": [], "securityContext": {"fsGroup": self._fs_group_value}}

    async def _patch_deployment(self, deployment: str, patch: dict[str, Any]) -> None:
        self.patches.append(patch)


def _mounted_volume(patched: _Patched) -> dict[str, Any]:
    volumes = patched.patches[0]["spec"]["template"]["spec"]["volumes"]
    volume: dict[str, Any] = volumes[0]
    return volume


async def test_a_file_apchi_mounts_is_restricted_where_the_pod_has_a_group() -> None:
    kubernetes = _Patched(fs_group=1000)

    await kubernetes.mount_secret_file(
        "trino-coordinator",
        "trino",
        "event-listener",
        "trino-event-listener",
        "/etc/trino/event-listener.properties",
        "event-listener.properties",
    )

    assert _mounted_volume(kubernetes)["secret"]["defaultMode"] == CREDENTIAL_FILE_MODE


async def test_a_file_is_left_alone_where_the_pod_has_no_group() -> None:
    """The careful half. Apchi does not own the Admin's pod template, and a root-owned 0640
    file with no group Trino belongs to is a file it cannot read -- which for
    `event-listener.properties` is a coordinator that will not start. Hardening a file into
    an outage is worse than the exposure, so the mode is left to the kubelet's default and
    the deployment is the thing to fix."""
    kubernetes = _Patched(fs_group=None)

    await kubernetes.mount_secret_file(
        "trino-coordinator",
        "trino",
        "event-listener",
        "trino-event-listener",
        "/etc/trino/event-listener.properties",
        "event-listener.properties",
    )

    assert "defaultMode" not in _mounted_volume(kubernetes)["secret"]


def test_the_validation_probe_restricts_the_files_it_holds() -> None:
    """The probe holds the same files as the Cluster, private keys included. Unlike the
    Cluster's pod template this one is Apchi's, so it simply sets both halves."""
    manifest = validation_manifest(
        "apchi-validate-probe", TRINO_IMAGE, 600, {"/etc/trino/certs/finance.key": "key material"}
    )

    spec = manifest["spec"]
    files = next(v for v in spec["volumes"] if v["name"] == "section-files")
    assert files["secret"]["defaultMode"] == CREDENTIAL_FILE_MODE
    # Without the group the mode above is root-only and the probe cannot read its own
    # configuration, which would fail every Validation for a reason that is not the
    # Candidate's.
    assert spec["securityContext"]["fsGroup"] == 1000


def test_a_probe_with_no_section_files_still_declares_the_group() -> None:
    """Nothing to restrict, and the group costs nothing -- but a probe that declared it
    only sometimes would behave differently depending on which Sections were staged."""
    manifest = validation_manifest("apchi-validate-probe", TRINO_IMAGE, 600, {})

    assert manifest["spec"]["securityContext"]["fsGroup"] == 1000
    assert not [v for v in manifest["spec"]["volumes"] if v["name"] == "section-files"]


@pytest.mark.parametrize("mode", [0o644, 0o666, 0o604, 0o660, 0o641])
def test_the_modes_libpq_refuses_are_the_ones_with_group_or_world_access(mode: int) -> None:
    """The rule, written out, because it is the reason for the constant above. libpq refuses
    a root-owned key file with any world bit, group write or group execute -- and nothing
    else. Reproduced from `fe-secure-openssl.c`: the mask is `S_IWGRP | S_IXGRP | S_IRWXO`
    for a root-owned file."""
    assert mode & 0o037, f"{mode:#o} should be a mode libpq refuses"
    assert mode != CREDENTIAL_FILE_MODE


def _with_volume(spec: dict[str, Any], volume: dict[str, Any]) -> dict[str, Any]:
    """The healthy spec plus one more volume, mounted in the trino container."""
    spec["volumes"].append(volume)
    spec["containers"][0]["volumeMounts"].append(
        {"name": volume["name"], "mountPath": f"/etc/trino/{volume['name']}"}
    )
    return spec


def _volume(spec: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(v for v in spec["volumes"] if v["name"] == name)
    return found


async def test_the_dev_deployment_restricts_its_credentials(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The baseline, so every refusal below is the violation it describes and not the fake
    being wrong about the Cluster it stands in for."""
    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_a_pod_with_no_group_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The half Apchi cannot fix for itself. Without an `fsGroup` there is no group Trino
    belongs to, so a credential file can only be world-readable or unreadable -- and the
    pod's securityContext is the Admin's to set."""
    spec = healthy_pod_spec()
    del spec["securityContext"]

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "fsGroup" in str(raised.value)
    assert "catalog-seed" in str(raised.value)


async def test_an_admin_credential_volume_with_no_mode_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """#182's case, and the one the official Trino chart produces: `secretMounts` renders
    only a name and a secretName, so the private keys arrive 0644."""
    spec = _with_volume(
        healthy_pod_spec(),
        {
            "name": "client-certificates",
            "secret": {"secretName": settings.client_certificate_secret_name},
        },
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "client-certificates" in str(raised.value)
    assert "defaultMode: 0640" in str(raised.value)


@pytest.mark.parametrize("mode", [0o644, 0o666, 0o660, 0o604])
async def test_a_mode_libpq_refuses_is_refused_here_too(
    mode: int, settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    spec = _with_volume(
        healthy_pod_spec(),
        {
            "name": "client-certificates",
            "secret": {
                "secretName": settings.client_certificate_secret_name,
                "defaultMode": mode,
            },
        },
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert f"{mode:#o}" in str(raised.value)


async def test_the_restricted_mode_is_accepted(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    spec = _with_volume(
        healthy_pod_spec(),
        {
            "name": "client-certificates",
            "secret": {
                "secretName": settings.client_certificate_secret_name,
                "defaultMode": CREDENTIAL_FILE_MODE,
            },
        },
    )

    await check(fake_kubernetes, pod_spec(spec), settings)


async def test_a_volume_apchi_mounts_itself_is_not_refused_for_its_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The deadlock this precondition has to avoid. Apchi sets the mode on its own mounts
    during Apply, so refusing one here would refuse the only thing that could fix it --
    and an upgrade from a version that mounted them bare would never get past this check.
    The `fsGroup` is what makes the next Apply restrict it, and that is checked above."""
    spec = healthy_pod_spec()
    spec["volumes"].append(
        {
            "name": settings.event_listener_volume_name,
            "secret": {"secretName": settings.event_listener_secret_name},
        }
    )
    spec["containers"][0]["volumeMounts"].append(
        {
            "name": settings.event_listener_volume_name,
            "mountPath": "/etc/trino/event-listener.properties",
            "subPath": "event-listener.properties",
        }
    )

    await check(fake_kubernetes, pod_spec(spec), settings)


async def test_a_volume_holding_no_credential_needs_no_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Access-control rules and mapping patterns are configuration, not secrets. Refusing
    a bare mode on those would make every deployment restrict files for no reason, and a
    check that fires where nothing is at stake is one an Admin learns to work around."""
    spec = healthy_pod_spec()

    assert "defaultMode" not in _volume(spec, "access-control")["secret"]
    assert "defaultMode" not in _volume(spec, "apchi-user-mapping")["secret"]
    await check(fake_kubernetes, pod_spec(spec), settings)


def test_only_the_sections_that_render_a_secret_declare_one(settings: Settings) -> None:
    """The list is the Sections' own, so this asserts what they say rather than restating
    it: a Section that starts rendering a password is covered without this test changing,
    and one that stops is noticed here."""
    assert credential_secrets(REGISTERED, settings) == {
        settings.client_certificate_secret_name,
        settings.event_listener_secret_name,
    }


def test_the_event_listener_directory_shape_carries_credentials_too(settings: Settings) -> None:
    """A listener's properties hold its password inline whether Apchi mounts the file or
    the Admin mounts the directory it sits in (#125), and only the second shape puts the
    mode in the Admin's hands."""
    from_deployment = settings.model_copy(
        update={"event_listener_config_files_from_deployment": True}
    )

    assert settings.event_listener_secret_name in credential_secrets(REGISTERED, from_deployment)


async def test_the_dev_worker_restricts_its_certificates(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The baseline for the second role, so the refusals below are the violations they
    describe."""
    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_an_exposed_key_on_a_worker_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """#184. A connector presenting a certificate to a data source runs on a worker
    (ADR-0005), so the chart mounts private keys there too -- and a key leaks on a worker
    exactly as it does on a coordinator."""
    assert fake_kubernetes.worker_pod_spec is not None
    _volume(fake_kubernetes.worker_pod_spec, "client-certificates")["secret"]["defaultMode"] = 0o644

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)

    assert "worker" in str(raised.value)
    assert "client-certificates" in str(raised.value)


async def test_a_worker_with_no_group_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The same half as on the coordinator, and for the same reason: the keys are
    root-owned, so without a group the worker cannot read them whatever the mode says."""
    assert fake_kubernetes.worker_pod_spec is not None
    del fake_kubernetes.worker_pod_spec["securityContext"]

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)

    assert "worker" in str(raised.value)
    assert "fsGroup" in str(raised.value)


async def test_both_roles_are_reported_together(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """§16 lists everything wrong rather than the first thing, and across roles too: an
    Admin fixing one chart should not have to Apply again to discover the other half."""
    spec = _with_volume(
        healthy_pod_spec(),
        {
            "name": "client-certificates",
            "secret": {
                "secretName": settings.client_certificate_secret_name,
                "defaultMode": 0o644,
            },
        },
    )
    assert fake_kubernetes.worker_pod_spec is not None
    _volume(fake_kubernetes.worker_pod_spec, "client-certificates")["secret"]["defaultMode"] = 0o644

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert len(raised.value.problems) == 2
    assert {"coordinator", "worker"} == {
        role for role in ("coordinator", "worker") if any(role in p for p in raised.value.problems)
    }


async def test_a_cluster_with_no_worker_deployment_is_not_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Whether the Cluster has workers at all is Verification's question, and it already
    answers it: `ready_replicas` reads the same Deployment on every Apply and raises on a
    name that does not resolve. A second owner for that requirement would mean two
    different failures for one misconfiguration, and this precondition is about file
    modes."""
    fake_kubernetes.worker_pod_spec = None

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_nothing_on_a_worker_is_exempt_as_apchis_own(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The coordinator's exemption exists because Apchi sets those modes during Apply, and
    Apply patches one Deployment. Nothing Apchi writes reaches a worker's pod template, so
    a volume named like one of Apchi's is still the Admin's here and still has to be
    restricted."""
    assert fake_kubernetes.worker_pod_spec is not None
    fake_kubernetes.worker_pod_spec["volumes"].append(
        {
            "name": settings.event_listener_volume_name,
            "secret": {"secretName": settings.event_listener_secret_name, "defaultMode": 0o644},
        }
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)

    assert settings.event_listener_volume_name in str(raised.value)
