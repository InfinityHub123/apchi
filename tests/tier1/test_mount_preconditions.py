"""The deployment shapes Apchi's own mounts can and cannot survive.

Apchi delivers four files as single-key `subPath` mounts it adds to the Admin's pod
template. Kubernetes cannot place one of those inside a read-only ConfigMap or Secret
directory mount: the key is not in that volume, so there is nothing to bind onto and runc
fails the container with "not a directory" before Trino runs. The official `trino/trino`
chart mounts the whole of `/etc/trino`, so this is the mainstream shape rather than an
exotic one -- and the failure has no diagnostic, because a pod that never starts logs
nothing.

So Apchi refuses it instead, while leaving the nested *directory* mounts that do layer
correctly alone -- the official chart's own `/etc/trino/catalog` is one of those.
"""

from typing import Any

import pytest

from app.config import Settings
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from app.sections.certificate_mapping.generator import MOUNT_PATH as MAPPING_PATH
from app.sections.event_listeners.generator import MOUNT_PATH as LISTENER_PATH
from app.sections.resource_groups.generator import MANAGER_PATH, RULES_PATH
from tests.conftest import FakeKubernetes, healthy_pod_spec

#: Every path Apchi mounts a single file at. Only MAPPING_PATH is mounted unconditionally;
#: the other three appear the first time an Operator configures their Section.
APCHI_FILES = (MAPPING_PATH, LISTENER_PATH, RULES_PATH, MANAGER_PATH)


def _with_mount(spec: dict[str, Any], mount: dict[str, Any], volume: dict[str, Any]) -> dict:
    spec["containers"][0]["volumeMounts"].append(mount)
    spec["volumes"].append(volume)
    return spec


def _whole_config_directory(path: str = "/etc/trino") -> dict[str, Any]:
    """The official chart's shape: one ConfigMap mount covering the config directory."""
    return _with_mount(
        healthy_pod_spec(),
        {"name": "etc-trino", "mountPath": path},
        {"name": "etc-trino", "configMap": {"name": "trino-coordinator"}},
    )


async def test_the_per_file_deployment_shape_is_accepted(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """deploy/trino-dev mounts each config file separately, so `/etc/trino` is the image's
    own directory and anything may be mounted into it."""
    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_a_whole_directory_config_mount_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The container would fail to start, before Trino logs anything, on an Apply that
    reported success."""
    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(_whole_config_directory()), settings)

    assert "not a directory" in str(raised.value)
    assert "/etc/trino" in str(raised.value)


async def test_the_refusal_names_every_file_apchi_would_mount_there(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Including the three whose mounts do not exist yet.

    Only the user-mapping file is mounted unconditionally. Checking what the current
    Candidate happens to mount would let a Cluster run for months and then crashloop on an
    Operator's first Event Listener -- the same failure, arriving later and looking like
    the Operator's doing.
    """
    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(_whole_config_directory()), settings)

    message = str(raised.value)
    for path in APCHI_FILES:
        assert path in message, f"{path} is mounted inside /etc/trino and was not named"


async def test_one_refusal_per_offending_mount_not_per_file(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Four files behind one mount is one thing to fix, so it reads as one problem."""
    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(_whole_config_directory()), settings)

    assert len(raised.value.problems) == 1, raised.value.problems


async def test_a_mount_above_the_config_directory_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """At or above: a ConfigMap over `/etc` makes `/etc/trino` read-only just as much."""
    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(_whole_config_directory("/etc")), settings)

    assert "not a directory" in str(raised.value)


async def test_a_nested_directory_mount_below_the_config_directory_is_accepted(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A directory inside a directory mount layers correctly -- verified with a two-volume
    pod, and relied on by the official chart's own /etc/trino/catalog. Apchi's two
    CoordinatorDirectory Sections are this shape, so refusing it would refuse Apchi."""
    spec = _with_mount(
        healthy_pod_spec(),
        {"name": "catalogs", "mountPath": "/etc/trino/catalog"},
        {"name": "catalogs", "configMap": {"name": "trino-catalogs"}},
    )

    await check(fake_kubernetes, pod_spec(spec), settings)


async def test_apchis_own_single_file_mount_does_not_trip_it(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The mount sits at the file path, not above the directory holding it. Tripping on
    Apchi's own mount would make the second Apply impossible after the first."""
    spec = healthy_pod_spec()
    mounts = spec["containers"][0]["volumeMounts"]
    assert any(mount["mountPath"] == MAPPING_PATH for mount in mounts), "fixture changed"

    await check(fake_kubernetes, pod_spec(spec), settings)


async def test_a_writable_directory_over_the_config_directory_is_accepted(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Scoped deliberately to read-only volumes, which is the case the "not a directory"
    failure was reproduced for. Whether the kubelet can create a bind target inside a
    writable emptyDir is unverified, and refusing a deployment that may work is the
    opposite of what the preconditions are for."""
    spec = _with_mount(
        healthy_pod_spec(),
        {"name": "scratch", "mountPath": "/etc/trino"},
        {"name": "scratch", "emptyDir": {}},
    )

    await check(fake_kubernetes, pod_spec(spec), settings)
