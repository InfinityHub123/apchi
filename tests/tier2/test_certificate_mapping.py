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


async def test_an_applied_pattern_in_force_on_the_real_cluster(
    e2e_client: AsyncClient, forward: PortForward, settings: Settings
) -> None:
    """The whole slice end to end -- staged, validated, written, rolled out, in force -- and
    what that does to three callers. One Apply, because each restarts the coordinator and each
    test pays a restart again to be put back (#300).

    - **A principal the pattern matches is mapped.** Insecure authentication takes the
      principal from `X-Trino-User`, which is also where the session user comes from -- so a
      pattern that renames leaves the two disagreeing, and Trino refuses the query as an
      impersonation attempt naming the mapped identity. With no pattern applied the principal
      would have stayed `Alice@example.com` and there would be nothing to impersonate. Under
      certificate authentication the two come from different places and a client simply
      presents its mapped name.
    - **Apchi can still reach a Cluster whose pattern excludes it.** Its reserved rule is
      first, and first match wins; Verification passing is already the proof.
    - **A principal no pattern matches keeps the name it presented.** Apchi's catch-all is
      last, so configuring a pattern does not start refusing everyone it does not cover.
    """
    await e2e_client.put("/api/v1/certificate-mapping", json=EXCLUDES_APCHI)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()

    def caller(user: str) -> Trino:
        return Trino(url=f"http://127.0.0.1:{forward.port}", user=user)

    with pytest.raises(TrinoUserError, match="User alice cannot impersonate"):
        await caller("Alice@example.com").query("SELECT current_user")
    assert await caller(settings.trino_user).query("SELECT current_user") == [[settings.trino_user]]
    assert await caller("nobody").query("SELECT current_user") == [["nobody"]]
