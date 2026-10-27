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
from app.pipeline.validation import validation_manifest

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
