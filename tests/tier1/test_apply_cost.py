"""What Review tells an Operator a Rollout will cost them.

Trino cannot drain a coordinator, so a Rollout destroys every running and queued query and
nothing Apchi does can soften that. The only honest response is to say so before the
Operator commits, with a number attached -- "this will kill 40 queries" and "this will kill
nothing" are different decisions.
"""

from httpx import AsyncClient

from app.pipeline.impact import RESTART_WARNING

CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}


async def test_a_catalogs_only_candidate_costs_nothing(applying_client: AsyncClient) -> None:
    """Half the Sections reach the Cluster without a restart, and Review should not imply
    otherwise."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["cost"]["restarts_coordinator"] is False
    assert review["cost"]["queries_at_risk"] is None
    assert review["cost"]["warning"] is None


async def test_a_listener_change_says_it_restarts_the_coordinator(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["cost"]["restarts_coordinator"] is True


async def test_the_warning_says_what_a_restart_does_to_queries(
    applying_client: AsyncClient,
) -> None:
    """In those words. "The cluster will restart" does not tell an Operator what it costs
    them."""
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)

    review = (await applying_client.get("/api/v1/review")).json()

    warning = review["cost"]["warning"]
    assert warning == RESTART_WARNING
    assert "terminates every running and queued query" in warning
    assert "cannot drain" in warning


async def test_the_count_comes_from_the_cluster_and_excludes_itself(
    applying_client: AsyncClient,
) -> None:
    """Nothing else is running against this coordinator, so the honest answer is zero --
    which it only is if the counting query does not count itself."""
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["cost"]["queries_at_risk"] == 0


async def test_review_still_works_when_the_cluster_cannot_be_asked(
    client: AsyncClient,
) -> None:
    """Review's job is to show what is staged. A Cluster that cannot be reached must not
    stop an Operator seeing their own changes."""
    await client.post("/api/v1/event-listeners", json=LISTENER)

    response = await client.get("/api/v1/review")

    assert response.status_code == 200
    review = response.json()
    assert review["cost"]["restarts_coordinator"] is True
    assert review["cost"]["queries_at_risk"] is None, "unknown, rather than guessed at"
    assert review["cost"]["warning"] == RESTART_WARNING
    listeners = next(s for s in review["sections"] if s["section"] == "event_listeners")
    assert [c["resource"] for c in listeners["changes"]] == ["audit"]


async def test_an_unchanged_listener_costs_nothing(applying_client: AsyncClient) -> None:
    """The Cluster having a listener is not a reason to warn about restarting it."""
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)
    started = await applying_client.post("/api/v1/applies")
    assert started.status_code == 202

    import asyncio

    from app.pipeline.applies import TERMINAL

    for _ in range(1200):
        record = (await applying_client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            break
        await asyncio.sleep(0.05)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["has_changes"] is False
    assert review["cost"]["restarts_coordinator"] is False


async def test_an_empty_candidate_costs_nothing(applying_client: AsyncClient) -> None:
    review = (await applying_client.get("/api/v1/review")).json()

    assert review["cost"] == {
        "restarts_coordinator": False,
        "queries_at_risk": None,
        "warning": None,
    }
