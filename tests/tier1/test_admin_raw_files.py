"""An Admin writes a path and its content, and three paths they cannot write at (§14).

The refusals are the point of this slice. Nothing here reaches the Cluster yet -- delivery,
validation and rollback are #158 -- so what is under test is the storage, the pre-image a
rollback will need, and the path rules, which are where the subtlety lives.

Each refusal exists because the alternative is worse than being told no:

* **a path Apchi owns** would silently replace generated configuration, and the worst case is
  specific -- overwriting the generated access-control rules turns invariant 8's fail-closed
  authorization into a silently open Cluster, with the Operator's grants still listed in the
  API so nothing looks wrong
* **a path the deployment cannot mount a single file at** would fail minutes later during an
  Apply, which for an Admin in the middle of an incident costs them the thing they were fixing
* **a path outside Trino's configuration directory** is not configuration

And the content is treated as a secret in its entirety, because Apchi cannot know which part
of an arbitrary file is a credential.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.auth.identity import Identity
from app.config import Settings
from app.main import create_app
from app.sections.raw_files import MAX_FILES
from tests.conftest import FakeKubernetes

FILES = "/api/v1/admin/files"
PASSWORD_IN_A_BLOB = "s3cret-platform-password"

ADMIN = Identity(subject="root", role="admin", via="password")
OPERATOR = Identity(subject="jo", role="operator", via="password")

#: An ordinary Trino configuration file with a credential in the middle of it. The redaction
#: filter keys on field *names*, so nothing here would catch this -- which is exactly why this
#: content is kept out of the log by origin instead.
CONTENT = f"""\
query.max-memory=50GB
query.max-memory-per-node=10GB
internal-communication.shared-secret={PASSWORD_IN_A_BLOB}
"""


async def _serving(
    settings: Settings, fake: FakeKubernetes, who: Identity
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
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


async def _write(http: AsyncClient, path: str, content: str = CONTENT) -> object:
    return await http.put(f"{FILES}{path}", json={"content": content})


# --- the round trip -----------------------------------------------------------------------


async def test_an_admin_writes_a_file_and_lists_it_back(as_admin: AsyncClient) -> None:
    written = await _write(as_admin, "/etc/trino/extra.properties")

    assert written.status_code == 200, written.text
    listed = (await as_admin.get(FILES)).json()
    assert listed == [{"path": "/etc/trino/extra.properties", "bytes": len(CONTENT.encode())}]


async def test_writing_the_same_path_twice_replaces(as_admin: AsyncClient) -> None:
    """Keyed by path rather than a list, because that is what a file means."""
    await _write(as_admin, "/etc/trino/extra.properties")

    await _write(as_admin, "/etc/trino/extra.properties", content="query.max-memory=1GB\n")

    listed = (await as_admin.get(FILES)).json()
    assert len(listed) == 1
    assert listed[0]["bytes"] == len(b"query.max-memory=1GB\n")


async def test_a_file_can_be_removed(as_admin: AsyncClient) -> None:
    await _write(as_admin, "/etc/trino/extra.properties")

    removed = await as_admin.delete(f"{FILES}/etc/trino/extra.properties")

    assert removed.status_code == 204, removed.text
    assert (await as_admin.get(FILES)).json() == []


async def test_removing_what_is_not_there_is_a_404(as_admin: AsyncClient) -> None:
    response = await as_admin.delete(f"{FILES}/etc/trino/never-written.properties")

    assert response.status_code == 404, response.text


# --- the content is a secret in its entirety ----------------------------------------------


async def test_a_listing_never_returns_content(as_admin: AsyncClient) -> None:
    """Deliberately not a round trip. Apchi cannot tell which part of an arbitrary file is a
    credential, so a listing that handed it back would make every reader of this endpoint a
    reader of those secrets."""
    await _write(as_admin, "/etc/trino/extra.properties")

    body = (await as_admin.get(FILES)).text

    assert PASSWORD_IN_A_BLOB not in body
    assert "query.max-memory" not in body


async def test_content_never_reaches_the_log(
    as_admin: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Kept out by origin, not by field name. The redaction filter matches names like
    `password`, and `internal-communication.shared-secret` inside an opaque blob is exactly
    what it would miss."""
    # Scoped to Apchi's own logger, because `at_level("DEBUG")` without one turns the *root*
    # logger up and with it PyMongo's command logging, which prints every document Apchi
    # writes. That is not a leak in production -- `configure_logging` sets the level on the
    # `app` namespace and leaves root at WARNING precisely so a dependency's debug output
    # cannot do this -- and `test_logging.py` now asserts that, because it is what keeps §12's
    # credentials out of the log for Snapshots too.
    with caplog.at_level("DEBUG", logger="app"):
        await _write(as_admin, "/etc/trino/extra.properties")

    assert PASSWORD_IN_A_BLOB not in caplog.text
    assert "query.max-memory" not in caplog.text
    # The path *is* logged, as structured data rather than in the message -- which `caplog.text`
    # does not render, so the record is where to look for it.
    logged = [getattr(record, "raw_file", None) for record in caplog.records]
    assert "/etc/trino/extra.properties" in logged, "the path should be logged"
    assert all(PASSWORD_IN_A_BLOB not in str(record.__dict__) for record in caplog.records), (
        "the content reached a record's structured fields"
    )


# --- the paths that are refused -----------------------------------------------------------


@pytest.mark.parametrize(
    "path,why",
    [
        ("/etc/trino/resource-groups.json", "a file Apchi mounts itself"),
        ("/etc/trino/event-listener.properties", "a file Apchi mounts itself"),
        ("/etc/trino/access-control/rules.json", "inside a directory Apchi fills"),
        ("/etc/trino/certs/anything.pem", "inside a directory Apchi fills"),
    ],
)
async def test_a_path_apchi_generates_is_refused_naming_the_owner(
    as_admin: AsyncClient, path: str, why: str
) -> None:
    """Two kinds, and both matter for different reasons.

    A file Apchi **mounts** would be a second mount at one path. Anything inside a directory
    Apchi **fills** would be clobbered by the next render or never appear -- the Admin mounts
    that Secret as a whole volume, so its contents are Apchi's.

    Checking only the first was the gap in this slice: `owned_paths` answers "what does Apchi
    mount", and the question is "what does Apchi generate", which is larger.
    """
    response = await _write(as_admin, path)

    assert response.status_code == 422, f"{path} ({why}) was accepted: {response.text}"
    assert "Apchi generates" in response.json()["message"]
    assert response.json()["details"][0]["property"] == "path"


async def test_the_admins_own_access_control_properties_is_writable(
    as_admin: AsyncClient,
) -> None:
    """Not everything near Apchi's files is Apchi's.

    `/etc/trino/access-control.properties` is the Admin's own file -- it points Trino at the
    rules directory Apchi fills -- so writing it raw is exactly the escape hatch working. If
    they leave out the refresh period, precondition 5 refuses the Apply, which is "if it works
    it works" doing its job rather than this endpoint second-guessing them.
    """
    response = await _write(
        as_admin,
        "/etc/trino/access-control.properties",
        content="access-control.name=file\nsecurity.refresh-period=1s\n",
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/data/trino/catalogs/x.properties",
        "/etc/trino",
    ],
    ids=["outside-entirely", "catalog-store", "the-directory-itself"],
)
async def test_a_path_outside_the_config_directory_is_refused(
    as_admin: AsyncClient, path: str
) -> None:
    """An escape hatch for Trino's configuration, not a way to write elsewhere in the
    container."""
    response = await _write(as_admin, path)

    assert response.status_code == 422, response.text
    assert "/etc/trino" in response.json()["message"]


async def test_a_path_cannot_escape_with_dot_dot(as_admin: AsyncClient) -> None:
    """Normalised before it is compared with anything. `/etc/trino/../passwd` and
    `/etc/passwd` are the same file, and a rule that only checked the string would have a
    hole in exactly this shape."""
    response = await _write(as_admin, "/etc/trino/../passwd")

    assert response.status_code == 422, response.text


async def test_a_normalised_path_is_what_is_stored(as_admin: AsyncClient) -> None:
    """So two spellings of one file are one file, and the stored key is what `owned_paths`
    would be compared against later."""
    await _write(as_admin, "/etc/trino/./sub/../extra.properties")

    listed = (await as_admin.get(FILES)).json()
    assert [f["path"] for f in listed] == ["/etc/trino/extra.properties"]


async def test_the_number_of_files_is_bounded(as_admin: AsyncClient) -> None:
    """An escape hatch with fifty files in it has stopped being an escape hatch."""
    for index in range(MAX_FILES):
        written = await _write(as_admin, f"/etc/trino/extra_{index}.properties")
        assert written.status_code == 200, written.text

    refused = await _write(as_admin, "/etc/trino/one_too_many.properties")

    assert refused.status_code == 422, refused.text
    assert str(MAX_FILES) in refused.json()["message"]


# --- the pre-image a rollback will need ---------------------------------------------------


async def test_the_previous_document_survives_a_write(as_admin: AsyncClient) -> None:
    """Auto Rollback restores what the Cluster had, and for an Admin Apply that is not a
    Snapshot -- Admin values never enter one (invariant 9). So the only thing a rollback can
    restore from is the document the last write replaced.

    Written by this slice even though #158 is what reads it: a pre-image added by the slice
    that needs it is a pre-image missing for everything written before.
    """
    app = as_admin._transport.app  # type: ignore[attr-defined]
    await _write(as_admin, "/etc/trino/extra.properties", content="first\n")

    await _write(as_admin, "/etc/trino/extra.properties", content="second\n")

    previous = await app.state.admin_store.previous()
    assert previous.raw_files["/etc/trino/extra.properties"] == "first\n"
    current = await app.state.admin_store.load()
    assert current.raw_files["/etc/trino/extra.properties"] == "second\n"


async def test_the_pre_image_is_empty_before_anything_is_written(as_admin: AsyncClient) -> None:
    """The honest answer for a Cluster whose Admin values have never been written: there is
    nothing to go back to, not a guess at what there might have been."""
    app = as_admin._transport.app  # type: ignore[attr-defined]

    previous = await app.state.admin_store.previous()

    assert previous.raw_files == {}


# --- who may do this ----------------------------------------------------------------------


async def test_an_operator_cannot_reach_these_routes(as_operator: AsyncClient) -> None:
    """§3: Admins inject low-level configuration. This is the sharpest form of that, so it is
    the one place where an Operator reaching it would matter most."""
    listed = await as_operator.get(FILES)
    written = await _write(as_operator, "/etc/trino/extra.properties")
    removed = await as_operator.delete(f"{FILES}/etc/trino/extra.properties")

    assert [listed.status_code, written.status_code, removed.status_code] == [403, 403, 403]


# --- a deployment that cannot take a raw file at all --------------------------------------


async def test_a_whole_directory_deployment_refuses_every_path_and_names_the_alternative(
    as_admin: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The shape the official `trino/trino` chart uses, and the one limit §14 cannot design
    away (#161).

    A raw file's path is the Admin's to choose, which makes it a single-file mount by nature;
    a deployment mounting the whole of `/etc/trino` as one volume cannot take one inside it,
    and runc fails the container with "not a directory" before Trino runs. There is no
    directory to move it into without removing the thing being asked for, so the only honest
    answer is to refuse and say what to use instead.

    The message is asserted, not just the status. An Admin mid-incident who is told "no" and
    nothing else has been handed a dead end; the chart's own `additionalConfigProperties` is
    the same capability by the route that deployment provides.
    """
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "etc-trino", "mountPath": "/etc/trino"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "etc-trino", "configMap": {"name": "trino-coordinator"}}
    )

    refused = await _write(as_admin, "/etc/trino/extra.properties")

    assert refused.status_code == 422, refused.text
    message = refused.json()["message"]
    assert "not a directory" in message
    assert "additionalConfigProperties" in message, "the refusal names no alternative"
    assert "official-trino-chart" in message, "the refusal points at no documentation"


async def test_config_properties_is_refused_there_too(
    as_admin: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The case that matters most, and the one that can never work on this shape: on such a
    deployment `config.properties` *is* the chart's own ConfigMap, so there is nothing for
    Apchi to replace."""
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "etc-trino", "mountPath": "/etc/trino"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "etc-trino", "configMap": {"name": "trino-coordinator"}}
    )

    refused = await _write(as_admin, "/etc/trino/config.properties")

    assert refused.status_code == 422, refused.text
