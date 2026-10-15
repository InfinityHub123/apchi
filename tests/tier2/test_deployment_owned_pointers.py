"""The deployment shape Apchi cannot mount a single file on, against a real cluster (#125).

Tier 1 proves the mechanics: which delivery each Section declares, where the rules are
rendered, that emptying is refused, and that the precondition catches a missing pointer. All
of that is checked against a fake Kubernetes, and a fake cannot prove the part the mode exists
for -- that **Trino reads the file from the directory the deployment pointed at**. A mode that
satisfies Apchi's own model and writes somewhere Trino never looks passes every tier 1 test
and does nothing, which is exactly the mistake `test_admin_raw_files.py` was written wrong
first time.

`deploy/trino-dev` mounts `/etc/trino` file by file, which is the shape where Apchi owns these
two mounts itself -- so this file reconfigures the coordinator into the other shape, runs one
Apply through it, and puts the cluster back. One file and few tests on purpose: the cost here
is two coordinator restarts, and what is under test is delivery rather than the breadth
`test_resource_groups.py` already covers in the default mode.

Resource Groups carries the proof because Trino reports what it did with the file:
`system.runtime.queries.resource_group_id` is non-null only if the rules were read. Event
Listeners cannot be proved that way -- a listener's output goes to an external sink and §8 is
explicit that Apchi asserts nothing functional about it -- so its half is the refusal to empty,
which is where the risk actually is.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline.applies import TERMINAL
from app.sections.event_listeners.generator import FILE_KEY as LISTENER_KEY
from app.sections.event_listeners.generator import MOUNT_DIR as LISTENER_DIR
from app.sections.event_listeners.generator import PATH_IN_DIR as LISTENER_IN_DIR
from app.sections.event_listeners.generator import render_properties
from app.sections.resource_groups.generator import (
    MANAGER_KEY,
    MANAGER_PATH,
    RULES_DIR,
    RULES_IN_DIR,
    RULES_KEY,
    render_rules,
)
from tests.tier2.conftest import (
    ForwardedKubernetes,
    PortForward,
    _restore_baseline,
    kubectl,
    running_apchi,
)

pytestmark = pytest.mark.tier2

GROUPS = "/api/v1/resource-groups"
LISTENERS = "/api/v1/event-listeners"

GROUP = {"path": "global", "hard_concurrency_limit": 100, "max_queued": 1000}
SELECTORS = {"selectors": [{"group": "global"}]}
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}

#: What the deployment provides in this mode. The resource groups pointer is a file of its own
#: because Trino rejects its keys in `config.properties`; the listener pointer lives in
#: `config.properties` because that is where Trino reads it.
MANAGER_FROM_DEPLOYMENT = (
    f"resource-groups.configuration-manager=file\nresource-groups.config-file={RULES_IN_DIR}\n"
)
LISTENER_POINTER = f"event-listener.config-files={LISTENER_IN_DIR}"

#: Content the Secrets must already hold before the coordinator restarts into this shape.
#:
#: Not a convenience. Trino validates every path property at startup and refuses to boot when
#: a file one names is missing -- reproduced while writing this: *"Invalid configuration
#: property with prefix '': file does not exist: /etc/trino/event-listener/..."*. Apchi writes
#: these Secrets on its first Apply, which is after. In the default mode their absence is how
#: "none configured" is expressed, which is why `deploy/trino-dev` ships them empty.
SEED = {
    "trino-resource-groups": {
        RULES_KEY: render_rules(
            {
                "global": {"hard_concurrency_limit": 100, "max_queued": 1000},
                "#selectors": [{"group": "global"}],
            }
        )
    },
    "trino-event-listener": {
        LISTENER_KEY: render_properties(
            {"type": "http", "properties": LISTENER["properties"]}  # type: ignore[arg-type]
        )
    },
}


def _apchi_mode_a_volumes() -> list[str]:
    """Which of Apchi's own single-file volumes the coordinator currently carries.

    They have to go: in this mode Apchi declares neither path, so a mount left behind from an
    earlier test would be a file at `/etc/trino/resource-groups.json` that nothing updates and
    nothing owns.
    """
    present = kubectl(
        "get",
        "deployment",
        "trino-coordinator",
        "-o",
        "jsonpath={.spec.template.spec.volumes[*].name}",
    ).split()
    return [v for v in ("apchi-resource-groups", "apchi-event-listener") if v in present]


def _mode_b_patch() -> dict:
    """The coordinator as a deployment that owns both pointers describes it.

    Strategic merge on `volumes` and `volumeMounts`, which merge by name and by mountPath --
    so this adds the three mounts without restating the ones `deploy/trino-dev` ships.
    """
    return {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [
                        {"name": "rg-rules", "secret": {"secretName": "trino-resource-groups"}},
                        {"name": "el-file", "secret": {"secretName": "trino-event-listener"}},
                    ],
                    "containers": [
                        {
                            "name": "trino",
                            "volumeMounts": [
                                # The Admin's pointer, from the ConfigMap they already mount.
                                {
                                    "name": "config",
                                    "mountPath": MANAGER_PATH,
                                    "subPath": MANAGER_KEY,
                                },
                                # The directories Apchi fills.
                                {"name": "rg-rules", "mountPath": RULES_DIR, "readOnly": True},
                                {"name": "el-file", "mountPath": LISTENER_DIR, "readOnly": True},
                            ],
                        }
                    ],
                }
            }
        }
    }


@pytest.fixture(scope="module")
def mode_b_cluster(cluster: None) -> Iterator[None]:
    """Reconfigure the coordinator to own both pointers, and put it back afterwards.

    Module scoped because the cost is two rollouts. Every test here runs against the one
    reconfigured cluster, and `_restore_baseline` is what the rest of the shard relies on --
    the same bargain `test_admin_raw_files.py` strikes for deliberately breaking the Cluster.
    """
    # Applied rather than created, so a Secret the manifests already ship is replaced rather
    # than refused.
    for name, data in SEED.items():
        kubectl(
            "apply",
            "-f",
            "-",
            stdin=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": name},
                    "stringData": data,
                }
            ),
        )

    config = json.loads(kubectl("get", "configmap", "trino-config", "-o", "json"))
    coordinator = config["data"]["coordinator-config.properties"]
    pointed_config = f"{coordinator.rstrip()}\n{LISTENER_POINTER}\n"
    kubectl(
        "patch",
        "configmap",
        "trino-config",
        "--type=merge",
        "-p",
        json.dumps(
            {
                "data": {
                    MANAGER_KEY: MANAGER_FROM_DEPLOYMENT,
                    "coordinator-config.properties": pointed_config,
                }
            }
        ),
    )

    if stale := _apchi_mode_a_volumes():
        from tests.tier2.conftest import _unmount_patch

        kubectl(
            "patch",
            "deployment",
            "trino-coordinator",
            "--type=strategic",
            "-p",
            json.dumps(_unmount_patch(stale)),
        )

    kubectl(
        "patch",
        "deployment",
        "trino-coordinator",
        "--type=strategic",
        "-p",
        json.dumps(_mode_b_patch()),
    )
    kubectl("rollout", "status", "deployment/trino-coordinator", "--timeout=600s")
    try:
        yield
    finally:
        _undo_mode_b()
        # Its own Settings, the way `baseline` does it: the test fixture's is function-scoped
        # and the MongoDB it names has no bearing on the state of the cluster. Default
        # settings on purpose -- the mode is a property of the deployment, and what this
        # asserts is that the deployment is back.
        _restore_baseline(Settings())


def _undo_mode_b() -> None:
    """Take the mounts and the pointer back off, explicitly.

    `_restore_baseline` is not enough on its own, and finding that out cost a crashlooping
    coordinator. It deletes the Secrets and re-applies the manifests -- but `kubectl apply`
    does a three-way merge, and a list entry or a map key added by `kubectl patch` is in
    neither the last-applied annotation nor the new configuration, so apply leaves it exactly
    where it is. That is the same reason `_restore_baseline` carries `_unmount_patch` for
    Apchi's own volumes rather than trusting apply to remove them.

    Left behind, the result is specific and fatal: `resource-groups.properties` still mounted,
    naming a rules file in a directory whose Secret apply has just emptied, so the coordinator
    will not start -- and every later test in the shard inherits that.
    """
    kubectl(
        "patch",
        "deployment",
        "trino-coordinator",
        "--type=strategic",
        "-p",
        json.dumps(
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [
                                {"name": "rg-rules", "$patch": "delete"},
                                {"name": "el-file", "$patch": "delete"},
                            ],
                            "containers": [
                                {
                                    "name": "trino",
                                    "volumeMounts": [
                                        {"mountPath": path, "$patch": "delete"}
                                        for path in (MANAGER_PATH, RULES_DIR, LISTENER_DIR)
                                    ],
                                }
                            ],
                        }
                    }
                }
            }
        ),
    )
    # A JSON patch, because strategic merge has no way to say "remove this map key" and the
    # manifest does not carry the key for apply to overwrite. Guarded on the key existing: a
    # `remove` of an absent path is an error, and teardown raising would bury whatever made
    # setup fail in the first place.
    config = json.loads(kubectl("get", "configmap", "trino-config", "-o", "json"))
    if MANAGER_KEY in (config.get("data") or {}):
        kubectl(
            "patch",
            "configmap",
            "trino-config",
            "--type=json",
            "-p",
            json.dumps([{"op": "remove", "path": f"/data/{MANAGER_KEY.replace('/', '~1')}"}]),
        )


@pytest.fixture
async def pointed_client(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    mode_b_cluster: None,
) -> AsyncIterator[AsyncClient]:
    """Apchi told that the deployment owns both pointers."""
    pointed = settings.model_copy(
        update={
            "resource_groups_properties_from_deployment": True,
            "event_listener_config_files_from_deployment": True,
        }
    )
    async with running_apchi(pointed, forwarded_kubernetes, forward) as client:
        yield client


async def _apply(client: AsyncClient, timeout: float = 600.0) -> dict:
    started = await client.post("/api/v1/applies", json={})
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(1.0)
    raise AssertionError("the apply never finished")


async def test_trino_reads_the_rules_from_the_directory_the_deployment_pointed_at(
    pointed_client: AsyncClient, forward: PortForward
) -> None:
    """The assertion the fake cannot make.

    Apchi writes no properties file in this mode -- the deployment's own names the directory --
    so the only thing that can make these rules take effect is Trino reading them from there.
    `resource_group_id` is non-null only if it did, which is the same evidence
    `test_resource_groups.py` uses for the default mode.
    """
    # Both Sections, because on this deployment both are mandatory. Staging only the one
    # under test was the first version of this test and the Apply was refused -- correctly:
    # an empty Event Listeners Section would have Apchi write an empty Secret over the file
    # `event-listener.config-files` names, and the next restart would not come back.
    assert (await pointed_client.post(GROUPS, json=GROUP)).status_code == 201
    assert (await pointed_client.put(f"{GROUPS}/selectors", json=SELECTORS)).status_code == 200
    assert (await pointed_client.post(LISTENERS, json=LISTENER)).status_code == 201

    record = await _apply(pointed_client)
    assert record["stage"] == "succeeded", record

    # On the live pod, not in Apchi's record of what it believes it wrote.
    listing = kubectl("exec", "deploy/trino-coordinator", "--", "ls", RULES_DIR)
    assert RULES_KEY in listing, listing
    assert "resource-groups.json" not in kubectl(
        "exec", "deploy/trino-coordinator", "--", "ls", "/etc/trino"
    ), "Apchi wrote the rules at its own path as well, which the deployment does not point at"

    assert LISTENER_KEY in kubectl("exec", "deploy/trino-coordinator", "--", "ls", LISTENER_DIR)

    trino = Trino(host="127.0.0.1", port=forward.port, user="apchi")
    assert await trino.query("SELECT 1") == [[1]]
    groups = await trino.query(
        "SELECT DISTINCT resource_group_id FROM system.runtime.queries "
        "WHERE resource_group_id IS NOT NULL"
    )
    assert groups, (
        "the coordinator attributed no query to a resource group, so it did not read the rules "
        f"Apchi wrote to {RULES_IN_DIR}"
    )


async def test_emptying_resource_groups_is_refused_on_this_deployment(
    pointed_client: AsyncClient,
) -> None:
    """The cost of the mode, against the cluster that pays it.

    The deployment's properties file always names a rules file, and Trino refuses to start
    when a file it was told to read is missing -- so removing the last group would leave a
    coordinator that cannot come back. Asserted here as well as in tier 1 because the premise
    is a property of this cluster rather than of Apchi.
    """
    await pointed_client.post(GROUPS, json=GROUP)

    refused = await pointed_client.delete(f"{GROUPS}/global")

    assert refused.status_code == 409, refused.text
    assert "resource-groups.config-file" in refused.json()["message"]
    # And the pointer really is what the message blames, on this cluster.
    assert MANAGER_FROM_DEPLOYMENT.strip() in kubectl(
        "exec", "deploy/trino-coordinator", "--", "cat", MANAGER_PATH
    )


async def test_emptying_event_listeners_is_refused_on_this_deployment(
    pointed_client: AsyncClient,
) -> None:
    """Trino has no listener that does nothing, so there is nothing honest to render in place
    of none -- which makes refusing the only safe answer here."""
    created = await pointed_client.post(LISTENERS, json=LISTENER)
    assert created.status_code == 201, created.text

    refused = await pointed_client.delete(f"{LISTENERS}/audit")

    assert refused.status_code == 409, refused.text
    assert "event-listener.config-files" in refused.json()["message"]
