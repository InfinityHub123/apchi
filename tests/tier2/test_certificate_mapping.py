"""The Certificate Mapping Pattern against a real cluster.

Tier 1 proves Apchi writes the file and asks for the Rollout. Only a real coordinator
proves the rest: that Java's regex engine, not Python's, has the last word; that a pattern
takes effect on the pod the Rollout brings up; and that Apchi's own reserved rule keeps it
able to reach a Cluster whose pattern excludes every name but its own.

The dev cluster authenticates insecurely, so the name a client sends is the principal the
mapping is applied to. That is the same `UserMapping` a certificate authenticator uses --
Trino refuses the certificate property without TLS, and nothing else about the file differs.
"""

import asyncio

import pytest
from httpx import AsyncClient
from trino.exceptions import TrinoUserError

from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

#: Valid in Python's `re`, rejected by `java.util.regex`, which knows no `(?P<name>...)`.
#: The kind of mistake static validation cannot catch and a probe can.
JAVA_REJECTS = {"pattern": "(?P<who>.*)@example\\.com", "user": "$1"}

#: Matches nobody Apchi authenticates as, which is the point: the reserved rule is what
#: keeps Verification able to reach the Cluster afterwards.
EXCLUDES_APCHI = {"pattern": "(.*)@example\\.com", "user": "$1", "case": "lower"}


async def _validation(client: AsyncClient, timeout: float = 600.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(1)
    raise AssertionError("Validation never finished")


async def _apply(client: AsyncClient, timeout: float = 900.0) -> dict:
    started = await client.post("/api/v1/applies")
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError("Apply never settled")


async def test_a_pattern_java_rejects_fails_validation_saying_why(e2e_client: AsyncClient) -> None:
    """Static validation passed it -- Python compiles the expression. The ephemeral
    coordinator is where it fails, which is before the Cluster has been touched."""
    staged = await e2e_client.put("/api/v1/certificate-mapping", json=JAVA_REJECTS)
    assert staged.status_code == 200

    verdict = await _validation(e2e_client)

    assert verdict["outcome"] == "failed"
    failure = verdict["failures"][0]
    assert failure["resource"] == "pattern", "the Operator is told what to fix"
    assert failure["reason"], "and given Trino's own words, which exist only in the pod's log"


async def test_an_applied_pattern_maps_a_principal_on_the_real_cluster(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The whole slice end to end: staged, validated, written, rolled out, in force.

    What the mapped identity looks like from outside takes a moment to explain. Insecure
    authentication takes the principal from `X-Trino-User`, which is also where the session
    user comes from -- so a pattern that renames leaves the two disagreeing, and Trino
    refuses the query as an impersonation attempt. The refusal names the identity the
    mapping produced, which is exactly what this test needs to see: with no pattern applied
    the principal would have stayed `Alice@example.com` and there would be nothing to
    impersonate. Under certificate authentication the two come from different places -- the
    certificate and the header -- and a client simply presents its mapped name.
    """
    await e2e_client.put("/api/v1/certificate-mapping", json=EXCLUDES_APCHI)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    alice = Trino(host="127.0.0.1", port=forward.port, user="Alice@example.com")
    with pytest.raises(TrinoUserError, match="User alice cannot impersonate"):
        await alice.query("SELECT current_user")


async def test_apchi_can_still_reach_a_cluster_whose_pattern_excludes_it(
    e2e_client: AsyncClient, forward: PortForward, settings: Settings
) -> None:
    """A principal no rule matches is denied, not passed through, so a pattern naming only
    the Operator's certificates would lock Apchi out of its own Cluster. Verification
    passing is the proof it does not: Apchi's reserved rule is first and matches itself."""
    await e2e_client.put("/api/v1/certificate-mapping", json=EXCLUDES_APCHI)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    apchi = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    assert await apchi.query("SELECT current_user") == [[settings.trino_user]]
    stranger = Trino(host="127.0.0.1", port=forward.port, user="nobody")
    with pytest.raises(Exception, match="(?i)mapping|authentication"):
        await stranger.query("SELECT 1")
