"""Client Certificates in the Configuration Candidate.

Upload is the interface, so most of what matters here is what Apchi refuses. A certificate
that is not a pair with its key becomes a Catalog that fails at handshake, somewhere else,
later, with nothing pointing back at the upload -- so the archive is understood at upload
time or refused at upload time.
"""

from cryptography.hazmat.primitives import serialization
from httpx import AsyncClient

from tests.certificates import bundle, certificate_pem, key_pair, key_pem, zipped


async def _upload(client: AsyncClient, name: str, archive: bytes) -> object:
    return await client.post(
        "/api/v1/certificates",
        data={"name": name},
        files={"archive": (f"{name}.zip", archive, "application/zip")},
    )


async def test_an_uploaded_bundle_is_stored_and_described(client: AsyncClient) -> None:
    created = await _upload(client, "finance", bundle())

    assert created.status_code == 201, created.json()
    body = created.json()
    assert body["name"] == "finance"
    assert body["common_name"] == "finance.clients.example.com"
    assert "Acme" in body["subject"]
    assert body["status"] == "valid"
    assert len(body["fingerprint"]) == 64


async def test_the_private_key_is_never_returned(client: AsyncClient) -> None:
    """An API that hands back key material makes every reader of every response a place it
    can leak from."""
    created = await _upload(client, "finance", bundle())
    listed = (await client.get("/api/v1/certificates")).json()

    assert "PRIVATE KEY" not in created.text
    assert "private_key" not in created.text
    assert "PRIVATE KEY" not in str(listed)


async def test_a_bundle_whose_parts_are_not_a_pair_is_refused(client: AsyncClient) -> None:
    certificate, _ = key_pair()
    _, other_key = key_pair("someone-else")
    archive = zipped({"c.pem": certificate_pem(certificate), "k.pem": key_pem(other_key)})

    refused = await _upload(client, "finance", archive)

    assert refused.status_code == 422
    assert "not a pair" in refused.json()["message"]


async def test_an_archive_with_no_certificate_is_refused(client: AsyncClient) -> None:
    _, key = key_pair()

    refused = await _upload(client, "finance", zipped({"k.pem": key_pem(key)}))

    assert refused.status_code == 422
    assert "no certificate" in refused.json()["message"]


async def test_an_archive_with_no_key_is_refused(client: AsyncClient) -> None:
    certificate, _ = key_pair()

    refused = await _upload(client, "finance", zipped({"c.pem": certificate_pem(certificate)}))

    assert refused.status_code == 422
    assert "no private key" in refused.json()["message"]


async def test_something_that_is_not_an_archive_is_refused(client: AsyncClient) -> None:
    refused = await _upload(client, "finance", b"this is not a zip at all")

    assert refused.status_code == 422
    assert "ZIP" in refused.json()["message"]


async def test_an_encrypted_key_is_refused_saying_so(client: AsyncClient) -> None:
    """Not malformed -- a key with a passphrase Apchi was not given. Saying "not a key"
    would send an Operator looking for the wrong problem."""
    certificate, key = key_pair()
    archive = zipped(
        {
            "c.pem": certificate_pem(certificate),
            "k.pem": key_pem(key, passphrase=b"secret"),
        }
    )

    refused = await _upload(client, "finance", archive)

    assert refused.status_code == 422
    assert "encrypted" in refused.json()["message"]


async def test_a_key_in_the_older_format_is_accepted(client: AsyncClient) -> None:
    """Uploads arrive as PKCS#1, as PKCS#8 and as DER. Apchi normalises rather than making
    every consumer of these files handle all three."""
    certificate, key = key_pair()
    archive = zipped(
        {
            "c.pem": certificate_pem(certificate),
            "k.pem": key_pem(key, serialization.PrivateFormat.TraditionalOpenSSL),
        }
    )

    created = await _upload(client, "finance", archive)

    assert created.status_code == 201, created.json()


async def test_a_bundle_carrying_its_ca_stores_the_leaf(client: AsyncClient) -> None:
    """A real bundle often includes the issuer. The certificate Trino should present is the
    one whose public key matches the key, which is how the leaf is picked out."""
    leaf, key = key_pair("leaf.example.com")
    ca, _ = key_pair("Acme Root CA")
    archive = zipped(
        {
            "leaf.pem": certificate_pem(leaf),
            "ca.pem": certificate_pem(ca),
            "k.pem": key_pem(key),
        }
    )

    created = await _upload(client, "finance", archive)

    assert created.status_code == 201, created.json()
    assert created.json()["common_name"] == "leaf.example.com"


async def test_an_expired_certificate_says_so(client: AsyncClient) -> None:
    created = await _upload(client, "stale", bundle(valid_for_days=-1))

    assert created.status_code == 201
    assert created.json()["status"] == "expired"


async def test_a_certificate_close_to_expiry_says_so(client: AsyncClient) -> None:
    created = await _upload(client, "soon", bundle(valid_for_days=5))

    assert created.json()["status"] == "expiring"


async def test_certificates_can_be_filtered_by_status(client: AsyncClient) -> None:
    """How an Operator finds what to renew before a Catalog stops connecting."""
    await _upload(client, "good", bundle())
    await _upload(client, "soon", bundle(valid_for_days=5))
    await _upload(client, "stale", bundle(valid_for_days=-1))

    expiring = (await client.get("/api/v1/certificates?status=expiring")).json()
    expired = (await client.get("/api/v1/certificates?status=expired")).json()

    assert [c["name"] for c in expiring] == ["soon"]
    assert [c["name"] for c in expired] == ["stale"]
    assert len((await client.get("/api/v1/certificates")).json()) == 3


async def test_uploading_under_an_existing_name_replaces_it(client: AsyncClient) -> None:
    """How renewal works. The Catalogs referencing it reference the name, so a renewal that
    needed a new name would mean editing every Catalog that uses it."""
    first = await _upload(client, "finance", bundle())
    second = await _upload(client, "finance", bundle())

    assert second.status_code == 201
    assert second.json()["fingerprint"] != first.json()["fingerprint"]
    assert len((await client.get("/api/v1/certificates")).json()) == 1


async def test_a_certificate_is_removed(client: AsyncClient) -> None:
    await _upload(client, "finance", bundle())

    removed = await client.delete("/api/v1/certificates/finance")

    assert removed.status_code == 204
    assert (await client.get("/api/v1/certificates")).json() == []


async def test_an_unknown_certificate_is_not_found(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/certificates/nope")).status_code == 404


async def test_a_name_that_is_not_safe_as_a_filename_is_refused(client: AsyncClient) -> None:
    """The name becomes a path in a Catalog's connector properties."""
    refused = await _upload(client, "../escape", bundle())

    assert refused.status_code == 422


async def test_certificates_appear_in_review_and_cost_no_restart(client: AsyncClient) -> None:
    """The directory is already mounted, so a certificate is a new file in it -- no pod spec
    change, and nobody's query dies because somebody uploaded a certificate."""
    await _upload(client, "finance", bundle())

    review = (await client.get("/api/v1/review")).json()

    certificates = next(s for s in review["sections"] if s["section"] == "client_certificates")
    assert [(c["resource"], c["change"]) for c in certificates["changes"]] == [("finance", "added")]
    assert review["cost"]["restarts_coordinator"] is False


async def test_reset_discards_staged_certificates(client: AsyncClient) -> None:
    await _upload(client, "finance", bundle())

    await client.post("/api/v1/candidate/reset")

    assert (await client.get("/api/v1/certificates")).json() == []


async def test_uploads_are_refused_under_maintenance_mode(client: AsyncClient) -> None:
    await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Trino upgrade"}
    )

    refused = await _upload(client, "finance", bundle())

    assert refused.status_code == 409
