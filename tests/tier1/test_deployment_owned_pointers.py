"""The deployment shape where Apchi cannot mount a single file (#125, §16).

Trino pins two files to its configuration directory and no property relocates either:
`resource-groups.properties` is read as `etc/resource-groups.properties`, and the alternative
to `etc/event-listener.properties` is `event-listener.config-files`, which lives in
`config.properties`. Apchi mounts both as single files, and a deployment that mounts the whole
of `/etc/trino` as one volume cannot accept one inside it -- runc fails the container with
"not a directory" before Trino runs. The official `trino/trino` chart is such a deployment.

So each of the two Sections gains a mode where the **deployment** provides the pointer and
Apchi fills only the directory it names. That is the division already in force for
`access-control.properties` (§13.4), and it changes nothing about who owns a Section: a pointer
names where a file lives and never varies with the Candidate, so Operators still own all six
(§3).

What it does cost is the ability to **empty** the Section, and that is what most of this file
is about. The pointer is always there, so the file it names has to be too -- and an Apply that
removed it would leave a coordinator that cannot come back.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.files import owned_paths
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from app.sections.base import CoordinatorDirectory
from app.sections.event_listeners.generator import MOUNT_DIR as LISTENER_DIR
from app.sections.event_listeners.generator import MOUNT_PATH as LISTENER_PATH
from app.sections.event_listeners.generator import PATH_IN_DIR as LISTENER_IN_DIR
from app.sections.registry import REGISTERED
from app.sections.resource_groups.generator import MANAGER_PATH, RULES_DIR, RULES_IN_DIR, RULES_PATH
from tests.conftest import FakeKubernetes, healthy_pod_spec

GROUPS = "/api/v1/resource-groups"
LISTENERS = "/api/v1/event-listeners"

GROUP = {"path": "global", "hard_concurrency_limit": 100, "max_queued": 1000}
OTHER_GROUP = {"path": "other", "hard_concurrency_limit": 10, "max_queued": 100}

#: The same group as the Candidate stores it. The path is the key there, not a field, so the
#: write model refuses it -- which is why the unit tests below cannot reuse `GROUP`.
STORED_GROUP = {"hard_concurrency_limit": 100, "max_queued": 1000}
STORED_LISTENER = {
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}

#: What the deployment provides in this mode, and where. `config.properties` carries the
#: listener pointer because that is where Trino reads it; the resource groups pointer is a
#: file of its own because Trino rejects its keys in `config.properties`.
MANAGER_FROM_DEPLOYMENT = (
    f"resource-groups.configuration-manager=file\nresource-groups.config-file={RULES_IN_DIR}\n"
)
LISTENER_POINTER = f"event-listener.config-files={LISTENER_IN_DIR}"


@pytest.fixture
def pointed(settings: Settings) -> Settings:
    """Both Sections in the mode where the deployment owns the pointer."""
    return settings.model_copy(
        update={
            "resource_groups_properties_from_deployment": True,
            "event_listener_config_files_from_deployment": True,
        }
    )


@pytest.fixture
def pointing_kubernetes(fake_kubernetes: FakeKubernetes) -> FakeKubernetes:
    """A Cluster whose deployment really does provide both pointers.

    Written into the ConfigMap the coordinator reads rather than asserted about, because the
    precondition under test follows the mount back to its ConfigMap and reads the property out
    of it -- the same thing it does for `security.refresh-period`.
    """
    data = fake_kubernetes.config_maps["trino-config"]
    data["resource-groups.properties"] = MANAGER_FROM_DEPLOYMENT
    data["coordinator-config.properties"] = f"coordinator=true\n{LISTENER_POINTER}\n"
    return fake_kubernetes


def _pointed_pod_spec() -> dict:
    """The whole-directory shape, with Apchi's two directories mounted inside it.

    Deliberately includes the `/etc/trino` ConfigMap mount that makes a single-file mount
    impossible, because a pod spec without it would let these tests pass in a shape that does
    not need this mode at all.
    """
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"].extend(
        [
            {"name": "etc-trino", "mountPath": "/etc/trino"},
            {"name": "apchi-resource-groups", "mountPath": RULES_DIR},
            {"name": "apchi-event-listener", "mountPath": LISTENER_DIR},
        ]
    )
    spec["volumes"].extend(
        [
            {"name": "etc-trino", "configMap": {"name": "trino-config"}},
            {
                "name": "apchi-resource-groups",
                "secret": {"secretName": "trino-resource-groups"},
            },
            {"name": "apchi-event-listener", "secret": {"secretName": "trino-event-listener"}},
        ]
    )
    return spec


@pytest.fixture
async def pointed_client(
    pointed: Settings, pointing_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    app = create_app(pointed)
    app.state.kubernetes = pointing_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


# --- what Apchi declares and writes -------------------------------------------------------


def test_neither_section_mounts_a_single_file_any_more(pointed: Settings) -> None:
    """The whole point. With nothing left in `owned_paths`, §16's single-file precondition
    has nothing to refuse -- without that check being weakened for anyone else, which is why
    the default mode is asserted alongside it."""
    assert owned_paths(REGISTERED, pointed) == {}

    default = pointed.model_copy(
        update={
            "resource_groups_properties_from_deployment": False,
            "event_listener_config_files_from_deployment": False,
        }
    )
    assert set(owned_paths(REGISTERED, default)) == {
        RULES_PATH,
        MANAGER_PATH,
        LISTENER_PATH,
    }


@pytest.mark.parametrize(
    "section_name,directory",
    [("resource_groups", RULES_DIR), ("event_listeners", LISTENER_DIR)],
)
def test_each_section_declares_a_directory_the_admin_mounts(
    pointed: Settings, section_name: str, directory: str
) -> None:
    section = next(s for s in REGISTERED if s.name == section_name)

    declared = section.coordinator_files(pointed)

    assert [type(spec) for spec in declared] == [CoordinatorDirectory]
    assert declared[0].path == directory


def test_the_rules_are_written_where_the_deployment_points(pointed: Settings) -> None:
    """And without the properties file, which is the deployment's now. Writing it anyway
    would be two files at one path, which precondition 3 refuses."""
    section = next(s for s in REGISTERED if s.name == "resource_groups")
    from app.sections.admin import AdminValues

    rendered = section.render_files({"global": STORED_GROUP}, pointed, AdminValues())

    assert set(rendered) == {RULES_IN_DIR}
    assert MANAGER_PATH not in rendered


def test_the_listener_is_written_where_the_deployment_points(pointed: Settings) -> None:
    section = next(s for s in REGISTERED if s.name == "event_listeners")
    from app.sections.admin import AdminValues

    rendered = section.render_files({"audit": STORED_LISTENER}, pointed, AdminValues())

    assert set(rendered) == {LISTENER_IN_DIR}


def test_the_probe_is_still_told_to_read_both(pointed: Settings) -> None:
    """The probe assembles its own configuration, so a pointer the Cluster's deployment
    provides has to be supplied to the probe by Apchi. Without it the probe would hold the
    files and not be told to read them, and every resource group and listener would pass
    (§6)."""
    groups = next(s for s in REGISTERED if s.name == "resource_groups")
    listeners = next(s for s in REGISTERED if s.name == "event_listeners")

    declared_groups = groups.coordinator_files(pointed)[0]
    declared_listeners = listeners.coordinator_files(pointed)[0]

    assert declared_groups.probe_files[MANAGER_PATH] == MANAGER_FROM_DEPLOYMENT
    assert declared_listeners.probe_config["event-listener.config-files"] == LISTENER_IN_DIR


# --- the precondition that the pointer is really there ------------------------------------


async def test_a_deployment_with_both_pointers_is_accepted(
    pointed: Settings, pointing_kubernetes: FakeKubernetes
) -> None:
    await check(pointing_kubernetes, pod_spec(_pointed_pod_spec()), pointed)


async def test_a_missing_resource_groups_pointer_is_refused(
    pointed: Settings, pointing_kubernetes: FakeKubernetes
) -> None:
    """An Admin who turns the mode on and forgets the file gets a Section that writes rules
    nobody reads: the Apply succeeds and the Cluster behaves as if it were empty. That is the
    failure shape §16 exists to catch."""
    del pointing_kubernetes.config_maps["trino-config"]["resource-groups.properties"]

    with pytest.raises(PreconditionFailed) as raised:
        await check(pointing_kubernetes, pod_spec(_pointed_pod_spec()), pointed)

    assert "resource_groups_properties_from_deployment" in str(raised.value)


async def test_a_missing_listener_pointer_is_refused(
    pointed: Settings, pointing_kubernetes: FakeKubernetes
) -> None:
    config = pointing_kubernetes.config_maps["trino-config"]["coordinator-config.properties"]
    pointing_kubernetes.config_maps["trino-config"]["coordinator-config.properties"] = (
        config.replace(f"{LISTENER_POINTER}\n", "")
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(pointing_kubernetes, pod_spec(_pointed_pod_spec()), pointed)

    assert "event-listener.config-files" in str(raised.value)


async def test_a_pointer_naming_somewhere_else_is_refused(
    pointed: Settings, pointing_kubernetes: FakeKubernetes
) -> None:
    """Worse than a missing one, because everything looks configured. Trino would read a file
    Apchi never writes, so the Section would look applied and have no effect."""
    pointing_kubernetes.config_maps["trino-config"]["resource-groups.properties"] = (
        "resource-groups.configuration-manager=file\n"
        "resource-groups.config-file=/etc/trino/somewhere-else.json\n"
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(pointing_kubernetes, pod_spec(_pointed_pod_spec()), pointed)

    assert "/etc/trino/somewhere-else.json" in str(raised.value)
    assert RULES_IN_DIR in str(raised.value)


async def test_the_pointers_are_not_demanded_in_the_default_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The check is scoped to the mode that needs it. A deployment where Apchi mounts the
    files itself has no reason to carry either pointer, and refusing it would make this a
    breaking change for every existing Cluster."""
    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


# --- the cost: the Sections can no longer be emptied --------------------------------------


async def test_removing_the_last_resource_group_is_refused(pointed_client: AsyncClient) -> None:
    """The pointer always names a rules file and Trino refuses to start when a file it was
    told to read is missing, so this would leave a coordinator that cannot come back.

    Refused at request time rather than only at Apply because the Candidate is shared: an
    empty Section that only Validation catches blocks everyone's Apply until somebody works
    out whose edit did it (ADR-0002).
    """
    created = await pointed_client.post(GROUPS, json=GROUP)
    assert created.status_code in (200, 201), created.text

    refused = await pointed_client.delete(f"{GROUPS}/global")

    assert refused.status_code == 409, refused.text
    assert "resource-groups.config-file" in refused.json()["message"]
    assert (await pointed_client.get(GROUPS)).json() != []


async def test_removing_a_resource_group_that_is_not_the_last_is_allowed(
    pointed_client: AsyncClient,
) -> None:
    """The refusal is about emptying the Section, not about deleting."""
    await pointed_client.post(GROUPS, json=GROUP)
    await pointed_client.post(GROUPS, json=OTHER_GROUP)

    removed = await pointed_client.delete(f"{GROUPS}/other")

    assert removed.status_code == 204, removed.text


async def test_removing_the_last_listener_is_refused(pointed_client: AsyncClient) -> None:
    """And here there is no second option, unlike Resource Groups: Trino's `http`, `mysql`
    and `kafka` listeners all need a real sink, so there is no listener that does nothing to
    render in place of none at all."""
    created = await pointed_client.post(LISTENERS, json=LISTENER)
    assert created.status_code in (200, 201), created.text

    refused = await pointed_client.delete(f"{LISTENERS}/audit")

    assert refused.status_code == 409, refused.text
    assert "event-listener.config-files" in refused.json()["message"]
    assert (await pointed_client.get(LISTENERS)).json() != []


async def test_emptying_is_allowed_in_the_default_mode(client: AsyncClient) -> None:
    """The asymmetry is the whole reason the mode exists. Where Apchi mounts the files
    itself, "none configured" is the mount not being there, and an Operator may still say
    it."""
    assert (await client.post(GROUPS, json=GROUP)).status_code in (200, 201)
    assert (await client.post(LISTENERS, json=LISTENER)).status_code in (200, 201)

    assert (await client.delete(f"{GROUPS}/global")).status_code == 204
    assert (await client.delete(f"{LISTENERS}/audit")).status_code == 204


# --- the net under the request-time refusal -----------------------------------------------

#: What each Section held before the Candidate was emptied, so `plan` reports a removal.
HELD = {
    "resource_groups": {"global": STORED_GROUP},
    "event_listeners": {"audit": STORED_LISTENER},
}


def _cluster(settings: Settings, kubernetes: FakeKubernetes) -> object:
    from app.sections.admin import AdminValues
    from app.sections.base import Cluster

    return Cluster(
        trino=Trino(host="127.0.0.1", port=1),
        kubernetes=kubernetes,
        settings=settings,
        admin=AdminValues(),
    )


@pytest.mark.parametrize("section_name", ["resource_groups", "event_listeners"])
async def test_an_empty_section_fails_validation_however_it_got_empty(
    pointed: Settings, pointing_kubernetes: FakeKubernetes, section_name: str
) -> None:
    """The DELETE refusal is not the only way in. A Section Revert to an empty Snapshot and a
    whole-document import both reach the Candidate without passing through it, so the rule is
    enforced at Apply time as well -- otherwise either path would produce a Cluster that
    cannot restart."""
    section = next(s for s in REGISTERED if s.name == section_name)

    failures = await section.check(
        _cluster(pointed, pointing_kubernetes), {}, section.plan({}, HELD[section_name])
    )

    assert [f.section for f in failures] == [section_name]
    assert "refuses to start" in failures[0].reason


@pytest.mark.parametrize("section_name", ["resource_groups", "event_listeners"])
async def test_the_refusal_is_the_state_and_not_only_the_transition(
    pointed: Settings, pointing_kubernetes: FakeKubernetes, section_name: str
) -> None:
    """Pinned, because scoping this to the transition is the tempting mistake and it is unsafe.

    Refusing only an Apply that *removes* the last resource looks kinder: an Apply touching
    nothing but catalogs would stop being blocked by a Section nobody asked about. But
    `files.by_secret` writes each Secret **whole**, so an Apply with this Section empty writes
    an empty Secret, the mounted directory loses the file the deployment's pointer names, and
    the next restart leaves a coordinator that cannot come back -- with Apchi having deleted
    the Admin's own seeded file on the way past.

    So an empty Section is refused whether or not this Apply is what emptied it. The test
    that caught the mistake was in tier 2, because only a real Cluster runs an Apply in this
    mode; this is the cheap guard that keeps it caught.
    """
    section = next(s for s in REGISTERED if s.name == section_name)

    # No removal in the plan: nothing was there to remove. Still refused.
    failures = await section.check(_cluster(pointed, pointing_kubernetes), {}, section.plan({}, {}))

    assert [f.section for f in failures] == [section_name]


@pytest.mark.parametrize("section_name", ["resource_groups", "event_listeners"])
async def test_an_empty_section_passes_validation_in_the_default_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes, section_name: str
) -> None:
    """Where Apchi mounts the files itself, an empty Section is the ordinary case -- the first
    Apply of a fresh Cluster has every Section empty, and emptying one is how an Operator
    turns it off."""
    section = next(s for s in REGISTERED if s.name == section_name)

    assert await section.check(_cluster(settings, fake_kubernetes), {}, section.plan({}, {})) == []
    assert (
        await section.check(
            _cluster(settings, fake_kubernetes), {}, section.plan({}, HELD[section_name])
        )
        == []
    )
