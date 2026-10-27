"""Every example in the API reference is a request the API actually accepts.

`test_api_examples.py` proves an example is valid for its Pydantic model, and -- for the two
Sections with a curated property schema -- that it passes that schema. Neither of those is
the handler. A body can satisfy both and still be refused: by a connector this Trino does
not have, by a cross-field rule, or by naming something that is not staged. The reader who
copied it is the one who finds out.

So each example is driven at the endpoint it is documented on, against a real Trino, with
whatever it references staged first. The ones that accumulate are then validated together
against a real coordinator, which is what proves the files they produce are files Trino
will start with.

The examples are read from `openapi.json` because that is the artefact a reader sees, and
every operation carrying one has to appear in `LOADERS` -- so a new example with nowhere to
run fails `test_every_documented_example_is_exercised` instead of being quietly skipped.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response

from tests.certificates import bundle

CONTRACT = Path(__file__).resolve().parents[2] / "openapi.json"

Example = dict[str, Any]
Loader = Callable[[AsyncClient, Example], Awaitable[Response]]


async def _upload_certificate(client: AsyncClient, name: str) -> None:
    uploaded = await client.post(
        "/api/v1/certificates",
        data={"name": name},
        files={"archive": (f"{name}.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.text


async def _stage_group(client: AsyncClient, path: str) -> None:
    """A group and every ancestor it needs, each with the one property Trino requires.

    Ancestors first: a child whose parent is not staged is refused, and the parent of
    `global.etl` is a group somebody has to have created.
    """
    segments = path.split(".")
    for depth in range(1, len(segments) + 1):
        ancestor = ".".join(segments[:depth])
        await client.post(
            "/api/v1/resource-groups",
            json={"path": ancestor, "hard_concurrency_limit": 5, "max_queued": 50},
        )


# --- one loader per documented operation ---------------------------------------


async def _create_catalog(client: AsyncClient, example: Example) -> Response:
    # The `certificate` example names a Client Certificate, and a Catalog naming one that
    # is not staged is refused -- correctly, which is why the example needs it uploaded.
    if example.get("certificate"):
        await _upload_certificate(client, example["certificate"])
    return await client.post("/api/v1/catalogs", json=example)


async def _patch_catalog(client: AsyncClient, example: Example) -> Response:
    """Patched onto a Catalog built to suit the example.

    A certificate only where the example mentions one, so `clear-certificate` has something
    to clear. Giving every base one is what a first draft of this did, and it made the
    `connector` example look broken: a Catalog holding a certificate cannot move to a
    connector Apchi has no certificate wiring for, which is correct and has nothing to do
    with changing a connector.
    """
    catalog: dict[str, Any] = {
        "name": "finance_pg",
        "connector": "postgresql",
        "properties": {
            "connection-url": "jdbc:postgresql://pg.finance.svc.cluster.local:5432/ledger",
            "connection-user": "trino_reader",
        },
    }
    if "certificate" in example:
        await _upload_certificate(client, "finance-client")
        catalog["certificate"] = "finance-client"
        # sslmode is required alongside a certificate and refused without one: a certificate
        # does nothing until the connection is told to use TLS.
        catalog["properties"]["connection-url"] += "?sslmode=verify-full"
    created = await client.post("/api/v1/catalogs", json=catalog)
    assert created.status_code == 201, created.text
    return await client.patch("/api/v1/catalogs/finance_pg", json=example)


async def _set_mapping(client: AsyncClient, example: Example) -> Response:
    return await client.put("/api/v1/certificate-mapping", json=example)


async def _create_listener(client: AsyncClient, example: Example) -> Response:
    return await client.post("/api/v1/event-listeners", json=example)


async def _patch_listener(client: AsyncClient, example: Example) -> Response:
    created = await client.post(
        "/api/v1/event-listeners",
        json={
            "name": "audit_sink",
            "type": "http",
            "properties": {
                "http-event-listener.connect-ingest-uri": "http://audit.svc.cluster.local:8080/t",
                "http-event-listener.log-completed": "true",
            },
        },
    )
    assert created.status_code == 201, created.text
    return await client.patch("/api/v1/event-listeners/audit_sink", json=example)


async def _add_grant(client: AsyncClient, example: Example) -> Response:
    return await client.post("/api/v1/permissions", json=example)


async def _set_privileges(client: AsyncClient, example: Example) -> Response:
    granted = await client.post(
        "/api/v1/permissions",
        json={
            "identity": "analytics-team",
            "catalog": "finance_pg",
            "schema": "reporting",
            "privileges": ["SELECT"],
        },
    )
    assert granted.status_code == 201, granted.text
    return await client.put(f"/api/v1/permissions/{granted.json()['key']}", json=example)


async def _add_group(client: AsyncClient, example: Example) -> Response:
    parent = example["path"].rpartition(".")[0]
    if parent:
        await _stage_group(client, parent)
    return await client.post("/api/v1/resource-groups", json=example)


async def _edit_group(client: AsyncClient, example: Example) -> Response:
    await _stage_group(client, "global")
    return await client.put("/api/v1/resource-groups/global", json=example)


async def _set_selectors(client: AsyncClient, example: Example) -> Response:
    """Every group the selectors name, staged first.

    Derived from the example rather than listed here, so a selector example pointing at a
    new group cannot turn into a Validation failure that looks like the example's fault.
    """
    for selector in example["selectors"]:
        await _stage_group(client, selector["group"])
    return await client.put("/api/v1/resource-groups/selectors", json=example)


async def _set_settings(client: AsyncClient, example: Example) -> Response:
    return await client.put("/api/v1/resource-groups/settings", json=example)


LOADERS: dict[tuple[str, str], Loader] = {
    ("POST", "/api/v1/catalogs"): _create_catalog,
    ("PATCH", "/api/v1/catalogs/{name}"): _patch_catalog,
    ("PUT", "/api/v1/certificate-mapping"): _set_mapping,
    ("POST", "/api/v1/event-listeners"): _create_listener,
    ("PATCH", "/api/v1/event-listeners/{name}"): _patch_listener,
    ("POST", "/api/v1/permissions"): _add_grant,
    ("PUT", "/api/v1/permissions/{key}"): _set_privileges,
    ("POST", "/api/v1/resource-groups"): _add_group,
    ("PUT", "/api/v1/resource-groups/{path}"): _edit_group,
    ("PUT", "/api/v1/resource-groups/selectors"): _set_selectors,
    ("PUT", "/api/v1/resource-groups/settings"): _set_settings,
}


def _documented() -> list[tuple[str, str, str, Example]]:
    """Every named request-body example in the contract, as (method, path, name, value)."""
    contract: dict[str, Any] = json.loads(CONTRACT.read_text())
    found: list[tuple[str, str, str, Example]] = []
    for path, operations in contract["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for media in (operation.get("requestBody") or {}).get("content", {}).values():
                for name, example in (media.get("examples") or {}).items():
                    found.append((method.upper(), path, name, example["value"]))
    return sorted(found)


DOCUMENTED = _documented()


def test_every_documented_example_is_exercised() -> None:
    """The guard that keeps this file honest: an operation whose examples nothing drives."""
    missing = sorted({(method, path) for method, path, _, _ in DOCUMENTED} - set(LOADERS))
    assert not missing, f"no loader for {missing}"


def test_every_section_has_examples() -> None:
    """Catalogs had them and the other Sections did not, which is what #178 was about.

    Client Certificates is absent on purpose: its write path is a multipart upload, so its
    example is on the form field rather than in a request body.
    """
    paths = {path for _, path, _, _ in DOCUMENTED}
    for section in (
        "catalogs",
        "certificate-mapping",
        "event-listeners",
        "permissions",
        "resource-groups",
    ):
        assert any(f"/api/v1/{section}" in path for path in paths), section


@pytest.mark.parametrize(
    ("method", "path", "name", "example"),
    DOCUMENTED,
    ids=[f"{method} {path} [{name}]" for method, path, name, _ in DOCUMENTED],
)
async def test_the_documented_example_is_accepted(
    applying_client: AsyncClient, method: str, path: str, name: str, example: Example
) -> None:
    """Driven against a real Trino, because some of what refuses a body needs one: a
    Catalog's connector is checked against the connectors this cluster actually has."""
    response = await LOADERS[(method, path)](applying_client, example)

    assert response.status_code in {200, 201}, (
        f"the {name!r} example on {method} {path} was refused with "
        f"{response.status_code}: {response.text}"
    )


#: The examples that build one configuration rather than showing alternatives. The others
#: are one-of by nature -- a second Event Listener is refused because Trino reads a single
#: listener file, and the Certificate Mapping Pattern is one rule -- so they cannot all be
#: staged at once and are proved one at a time by the test above.
TOGETHER: list[tuple[str, Example | None]] = [
    ("POST /api/v1/catalogs", {"example": "postgresql"}),
    ("POST /api/v1/catalogs", {"example": "uncurated"}),
    ("PUT /api/v1/certificate-mapping", {"example": "folded"}),
    ("POST /api/v1/event-listeners", {"example": "http"}),
    ("POST /api/v1/permissions", {"example": "one-table"}),
    ("POST /api/v1/permissions", {"example": "one-schema"}),
    ("POST /api/v1/permissions", {"example": "whole-catalog"}),
    ("POST /api/v1/resource-groups", {"example": "root"}),
    ("POST /api/v1/resource-groups", {"example": "child"}),
    ("PUT /api/v1/resource-groups/settings", {"example": "cpu-quota"}),
    ("POST /api/v1/resource-groups", {"example": "cpu-limited"}),
    ("PUT /api/v1/resource-groups/selectors", {"example": "ordered"}),
]


def _example(operation: str, name: str) -> Example:
    method, path = operation.split(" ", 1)
    for other_method, other_path, other_name, value in DOCUMENTED:
        if (other_method, other_path, other_name) == (method, path, name):
            return value
    raise AssertionError(f"no {name!r} example on {operation}")


async def _validation(client: AsyncClient, timeout: float = 120.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Validation never finished")


async def test_the_examples_validate_together_against_a_real_coordinator(
    applying_client: AsyncClient,
) -> None:
    """The proof that the examples are real rather than merely well-formed.

    The settings go in before the group that needs them: Trino refuses to start when a
    group sets a CPU limit and nothing set `cpuQuotaPeriod`, so the order here is the order
    an Operator has to follow too.

    Reachability is advisory by default (#177), so the hosts in these examples not existing
    is a warning rather than a failure -- which is the right outcome for an example, and
    `CREATE CATALOG` accepts an unreachable host anyway.
    """
    for operation, selection in TOGETHER:
        assert selection is not None
        method, path = operation.split(" ", 1)
        response = await LOADERS[(method, path)](
            applying_client, _example(operation, selection["example"])
        )
        assert response.status_code in {200, 201}, (
            f"{selection['example']!r} on {operation}: {response.status_code} {response.text}"
        )

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
