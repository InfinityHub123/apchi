"""Event Listeners in the Configuration Candidate.

Staging only. Nothing here reaches Trino or Kubernetes, which is the whole reason staging
is safe -- and this Section restarts the coordinator when it is applied, so that matters
more here than it did for Catalogs.
"""

from httpx import AsyncClient

HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/events"},
}
KAFKA = {
    "name": "pipeline",
    "type": "kafka",
    "properties": {
        "kafka-event-listener.broker-endpoints": "kafka-1:9093",
        "kafka-event-listener.created-event.topic": "created",
        "kafka-event-listener.completed-event.topic": "completed",
        "kafka-event-listener.client-id": "apchi-dev",
    },
}


async def test_a_staged_listener_is_listed(client: AsyncClient) -> None:
    created = await client.post("/api/v1/event-listeners", json=HTTP)

    assert created.status_code == 201
    listed = (await client.get("/api/v1/event-listeners")).json()
    assert [listener["name"] for listener in listed] == ["audit"]
    assert listed[0]["type"] == "http"


async def test_a_curated_listener_is_marked_supported(client: AsyncClient) -> None:
    created = (await client.post("/api/v1/event-listeners", json=KAFKA)).json()

    assert created["supported"] is True


async def test_an_uncurated_type_passes_through_marked_unsupported(client: AsyncClient) -> None:
    """Apchi checked nothing, and says so, rather than blocking a plugin the platform
    team installed."""
    created = await client.post(
        "/api/v1/event-listeners",
        json={"name": "custom", "type": "in_house_thing", "properties": {"whatever": "1"}},
    )

    assert created.status_code == 201
    assert created.json()["supported"] is False
    assert created.json()["properties"] == {"whatever": "1"}


async def test_a_missing_required_property_is_refused_with_its_name(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/event-listeners", json={"name": "audit", "type": "http", "properties": {}}
    )

    assert refused.status_code == 422
    assert refused.json()["code"] == "unprocessable_payload"
    problems = [detail["property"] for detail in refused.json()["details"]]
    assert "http-event-listener.connect-ingest-uri" in problems


async def test_kafka_requires_its_endpoints_topics_and_client_id(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/event-listeners",
        json={"name": "pipeline", "type": "kafka", "properties": {}},
    )

    assert refused.status_code == 422
    assert {detail["property"] for detail in refused.json()["details"]} == {
        "kafka-event-listener.broker-endpoints",
        "kafka-event-listener.created-event.topic",
        "kafka-event-listener.completed-event.topic",
        "kafka-event-listener.client-id",
    }


async def test_a_typo_is_refused_and_the_likely_property_suggested(client: AsyncClient) -> None:
    """Without the suggestion an Operator gets "unknown property" and has to go and read
    Trino's documentation."""
    refused = await client.post(
        "/api/v1/event-listeners",
        json={
            "name": "audit",
            "type": "http",
            "properties": {"http-event-listener.connect-ingest-url": "http://c:8080/e"},
        },
    )

    assert refused.status_code == 422
    problems = " ".join(detail["problem"] for detail in refused.json()["details"])
    assert "connect-ingest-uri" in problems


async def test_an_invalid_selector_value_is_refused(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/event-listeners",
        json={
            "name": "audit",
            "type": "http",
            "properties": {
                "http-event-listener.connect-ingest-uri": "http://c:8080/e",
                "http-event-listener.connect-http-method": "PATCH",
            },
        },
    )

    assert refused.status_code == 422
    problems = " ".join(detail["problem"] for detail in refused.json()["details"])
    assert "POST" in problems and "PUT" in problems


async def test_a_second_listener_is_refused_with_the_reason(client: AsyncClient) -> None:
    """Trino reads one listener file by default; more than one needs Admin-owned
    configuration Apchi does not write."""
    await client.post("/api/v1/event-listeners", json=HTTP)

    refused = await client.post("/api/v1/event-listeners", json=KAFKA)

    assert refused.status_code == 409
    assert refused.json()["code"] == "too_many_event_listeners"
    assert "event-listener.config-files" in refused.json()["message"]


async def test_the_one_listener_can_be_replaced(client: AsyncClient) -> None:
    """The limit must not trap an Operator: removing then adding has to work."""
    await client.post("/api/v1/event-listeners", json=HTTP)
    await client.delete("/api/v1/event-listeners/audit")

    added = await client.post("/api/v1/event-listeners", json=KAFKA)

    assert added.status_code == 201


async def test_a_listener_can_be_repointed_without_recreating_it(client: AsyncClient) -> None:
    await client.post("/api/v1/event-listeners", json=HTTP)

    updated = await client.patch(
        "/api/v1/event-listeners/audit",
        json={"properties": {"http-event-listener.connect-ingest-uri": "http://new:9090/e"}},
    )

    assert updated.status_code == 200
    assert updated.json()["properties"] == {
        "http-event-listener.connect-ingest-uri": "http://new:9090/e"
    }


async def test_an_edit_is_validated_too(client: AsyncClient) -> None:
    await client.post("/api/v1/event-listeners", json=HTTP)

    refused = await client.patch(
        "/api/v1/event-listeners/audit", json={"properties": {"nonsense": "1"}}
    )

    assert refused.status_code == 422


async def test_a_removed_listener_is_gone(client: AsyncClient) -> None:
    await client.post("/api/v1/event-listeners", json=HTTP)

    removed = await client.delete("/api/v1/event-listeners/audit")

    assert removed.status_code == 204
    assert (await client.get("/api/v1/event-listeners")).json() == []


async def test_an_unknown_listener_is_not_found(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/event-listeners/nope")

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_listeners_appear_in_review_beside_catalogs(client: AsyncClient) -> None:
    await client.post("/api/v1/event-listeners", json=HTTP)

    review = (await client.get("/api/v1/review")).json()

    listeners = next(s for s in review["sections"] if s["section"] == "event_listeners")
    assert [(c["resource"], c["change"]) for c in listeners["changes"]] == [("audit", "added")]
    assert review["has_changes"] is True


async def test_reset_discards_staged_listeners(client: AsyncClient) -> None:
    await client.post("/api/v1/event-listeners", json=HTTP)

    await client.post("/api/v1/candidate/reset")

    assert (await client.get("/api/v1/event-listeners")).json() == []


async def test_listener_mutations_are_refused_under_maintenance_mode(client: AsyncClient) -> None:
    await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Trino upgrade"}
    )

    refused = await client.post("/api/v1/event-listeners", json=HTTP)

    assert refused.status_code == 409
    assert refused.json()["code"] == "maintenance_mode"


async def test_reads_are_unaffected_by_maintenance_mode(client: AsyncClient) -> None:
    await client.put("/api/v1/admin/maintenance-mode", json={"engaged": True})

    assert (await client.get("/api/v1/event-listeners")).status_code == 200
