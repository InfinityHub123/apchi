"""The Certificate Mapping Pattern in the Configuration Candidate.

Staging only: nothing here reaches Trino or Kubernetes. One pattern, so one resource --
there is no collection to list and no name to address it by.
"""

from httpx import AsyncClient

PATTERN = {"pattern": "(.*)@example\\.com", "user": "$1"}


async def test_no_pattern_is_configured_to_begin_with(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/certificate-mapping")

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_a_staged_pattern_is_returned(client: AsyncClient) -> None:
    saved = await client.put("/api/v1/certificate-mapping", json=PATTERN)

    assert saved.status_code == 200
    assert (await client.get("/api/v1/certificate-mapping")).json() == {
        "pattern": "(.*)@example\\.com",
        "user": "$1",
        "case": "keep",
    }


async def test_setting_it_again_replaces_it(client: AsyncClient) -> None:
    """There is only ever one, so a second PUT is an edit rather than a conflict."""
    await client.put("/api/v1/certificate-mapping", json=PATTERN)

    replaced = await client.put(
        "/api/v1/certificate-mapping",
        json={"pattern": "CN=(.*?),.*", "user": "$1", "case": "lower"},
    )

    assert replaced.status_code == 200
    assert replaced.json()["case"] == "lower"
    assert (await client.get("/api/v1/certificate-mapping")).json()["pattern"] == "CN=(.*?),.*"


async def test_a_pattern_that_is_not_an_expression_is_refused(client: AsyncClient) -> None:
    refused = await client.put("/api/v1/certificate-mapping", json={"pattern": "(unclosed"})

    assert refused.status_code == 422
    assert refused.json()["code"] == "unprocessable_payload"
    assert [detail["property"] for detail in refused.json()["details"]] == ["pattern"]


async def test_a_replacement_referring_to_a_group_that_does_not_exist_is_refused(
    client: AsyncClient,
) -> None:
    """Trino substitutes nothing and the principal ends up with no identity at all, which
    reads at the far end as an authentication failure nobody can explain."""
    refused = await client.put(
        "/api/v1/certificate-mapping", json={"pattern": ".*@example.com", "user": "$1"}
    )

    assert refused.status_code == 422
    problems = " ".join(detail["problem"] for detail in refused.json()["details"])
    assert "capturing group 1" in problems


async def test_an_unknown_case_treatment_is_refused(client: AsyncClient) -> None:
    refused = await client.put(
        "/api/v1/certificate-mapping", json={"pattern": "(.*)", "case": "title"}
    )

    assert refused.status_code == 422


async def test_a_removed_pattern_is_gone(client: AsyncClient) -> None:
    await client.put("/api/v1/certificate-mapping", json=PATTERN)

    removed = await client.delete("/api/v1/certificate-mapping")

    assert removed.status_code == 204
    assert (await client.get("/api/v1/certificate-mapping")).status_code == 404


async def test_removing_a_pattern_that_is_not_there_is_not_found(client: AsyncClient) -> None:
    missing = await client.delete("/api/v1/certificate-mapping")

    assert missing.status_code == 404


async def test_the_pattern_appears_in_review_and_restarts_the_coordinator(
    client: AsyncClient,
) -> None:
    await client.put("/api/v1/certificate-mapping", json=PATTERN)

    review = (await client.get("/api/v1/review")).json()

    mapping = next(s for s in review["sections"] if s["section"] == "certificate_mapping")
    assert [(c["resource"], c["change"]) for c in mapping["changes"]] == [("pattern", "added")]
    assert review["cost"]["restarts_coordinator"] is True


async def test_reset_discards_a_staged_pattern(client: AsyncClient) -> None:
    await client.put("/api/v1/certificate-mapping", json=PATTERN)

    await client.post("/api/v1/candidate/reset")

    assert (await client.get("/api/v1/certificate-mapping")).status_code == 404


async def test_pattern_mutations_are_refused_under_maintenance_mode(client: AsyncClient) -> None:
    await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Trino upgrade"}
    )

    refused = await client.put("/api/v1/certificate-mapping", json=PATTERN)

    assert refused.status_code == 409
    assert (await client.get("/api/v1/certificate-mapping")).status_code == 404
