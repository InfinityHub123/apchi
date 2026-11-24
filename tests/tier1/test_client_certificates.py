"""Client Certificates in the Configuration Candidate.

Upload is the interface, so most of what matters here is what Apchi refuses. A certificate
that is not a pair with its key becomes a Catalog that fails at handshake, somewhere else,
later, with nothing pointing back at the upload -- so the archive is understood at upload
time or refused at upload time.
"""

from collections.abc import AsyncIterator

import pytest
from cryptography.hazmat.primitives import serialization
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from tests.certificates import bundle, certificate_pem, key_pair, key_pem, zipped
from tests.conftest import FakeKubernetes


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


# --- inspection ----------------------------------------------------------------------------


async def _inspect(client: AsyncClient, name: str, archive: bytes) -> object:
    return await client.post(
        "/api/v1/certificates/inspect",
        data={"name": name},
        files={"archive": (f"{name}.zip", archive, "application/zip")},
    )


async def test_an_inspection_describes_the_archive_and_stages_nothing(
    client: AsyncClient,
) -> None:
    """Before this, the only way to find out what was in a ZIP was to stage it -- and under
    an existing name that replaced a working certificate straight away."""
    reading = await _inspect(client, "finance", bundle())

    assert reading.status_code == 200, reading.text
    body = reading.json()
    assert body["certificate_member"] == "client.crt"
    assert body["key_member"] == "client.key"
    assert body["certificate"]["common_name"] == "finance.clients.example.com"
    assert body["certificate"]["status"] == "valid"
    assert body["replaces"] is None, "the name is new"
    assert body["used_by"] == []
    assert (await client.get("/api/v1/certificates")).json() == [], "nothing was staged"


async def test_it_names_which_member_it_took_for_what(client: AsyncClient) -> None:
    """Members are identified by what they parse as, never by their name -- a ZIP from one
    tool calls the key `privkey.pk8` and from another `client-key.pem`. So the reading has to
    say which member it chose, because that is the only way somebody notices they zipped the
    issuing CA instead of their leaf."""
    certificate, key = key_pair()
    archive = zipped({"muddled-name.dat": key_pem(key), "other.bin": certificate_pem(certificate)})

    body = (await _inspect(client, "finance", archive)).json()

    assert body["certificate_member"] == "other.bin"
    assert body["key_member"] == "muddled-name.dat"


async def test_it_reports_what_would_be_converted(client: AsyncClient) -> None:
    """A PKCS#1 key is stored as PKCS#8 and a DER certificate as PEM. Worth reporting
    because the stored bytes then differ from the uploaded ones, and somebody comparing a
    fingerprint against their own file needs to know Apchi normalised it."""
    certificate, key = key_pair()
    archive = zipped(
        {
            "c.der": certificate.public_bytes(serialization.Encoding.DER),
            "k.pem": key_pem(key, serialization.PrivateFormat.TraditionalOpenSSL),
        }
    )

    body = (await _inspect(client, "finance", archive)).json()

    assert body["conversions"] == [
        "the certificate arrived as DER and is stored as PEM",
        "the private key arrived as PKCS#1 PEM and is stored as PKCS#8 PEM",
    ]


async def test_an_ordinary_bundle_reports_no_conversions(client: AsyncClient) -> None:
    """Empty is its own answer, not missing information."""
    body = (await _inspect(client, "finance", bundle())).json()

    assert body["conversions"] == []


async def test_it_reports_what_the_upload_would_replace(client: AsyncClient) -> None:
    """The field the endpoint exists for. Uploading under an existing name replaces it
    immediately, so comparing the two expiries is how somebody confirms a renewal is a
    renewal rather than a mistake."""
    assert (await _upload(client, "finance", bundle(valid_for_days=10))).status_code == 201

    body = (await _inspect(client, "finance", bundle(valid_for_days=400))).json()

    assert body["replaces"]["name"] == "finance"
    assert body["replaces"]["status"] == "expiring", "the one on its way out"
    assert body["certificate"]["status"] == "valid", "the one that would take its place"
    assert body["replaces"]["fingerprint"] != body["certificate"]["fingerprint"]

    staged = (await client.get("/api/v1/certificates")).json()
    assert len(staged) == 1
    assert staged[0]["fingerprint"] == body["replaces"]["fingerprint"], "still the old one"


async def test_it_names_the_catalogs_that_would_be_repointed(
    applying_client: AsyncClient,
) -> None:
    """Replacing a certificate re-points every Catalog presenting it, which is how renewal is
    meant to work and is also what makes a wrong upload reach past the certificate.

    `applying_client` because creating a Catalog asks the coordinator whether the connector
    exists, and this test needs a real Catalog rather than a refusal.
    """
    await _upload(applying_client, "finance", bundle())
    created = await applying_client.post(
        "/api/v1/catalogs",
        json={
            "name": "finance_pg",
            "connector": "postgresql",
            "properties": {
                "connection-url": "jdbc:postgresql://pg:5432/ledger?sslmode=verify-full"
            },
            "certificate": "finance",
        },
    )
    assert created.status_code == 201, created.text

    body = (await _inspect(applying_client, "finance", bundle())).json()

    assert body["used_by"] == ["finance_pg"]


async def test_an_inspection_refuses_exactly_what_the_upload_refuses(
    client: AsyncClient,
) -> None:
    """An inspection that said yes where the upload says no would be worse than none at all:
    it is read precisely to find out whether the upload is safe."""
    certificate, _ = key_pair()
    _, other_key = key_pair("someone-else")
    mismatched = zipped({"c.pem": certificate_pem(certificate), "k.pem": key_pem(other_key)})

    inspected = await _inspect(client, "finance", mismatched)
    uploaded = await _upload(client, "finance", mismatched)

    assert inspected.status_code == uploaded.status_code == 422
    assert inspected.json()["message"] == uploaded.json()["message"]


async def test_inspecting_is_not_a_way_round_a_bad_archive(client: AsyncClient) -> None:
    """The refusals that belong to the reader rather than to the Section."""
    assert (await _inspect(client, "finance", b"not a zip at all")).status_code == 422
    assert (await _inspect(client, "finance", zipped({"c.pem": b"junk"}))).status_code == 422


async def test_an_inspection_never_returns_key_material(client: AsyncClient) -> None:
    """It reads a private key to prove the pair, which makes it one more response that could
    leak one."""
    reading = await _inspect(client, "finance", bundle())

    assert "PRIVATE KEY" not in reading.text
    assert "private_key" not in reading.text


async def test_an_inspection_records_nothing_in_the_audit(client: AsyncClient) -> None:
    """It changed nothing. The audit is written when the Candidate is saved, and an
    inspection never saves it."""
    before = (await client.get("/api/v1/audit", params={"category": "client_certificates"})).json()

    await _inspect(client, "finance", bundle())

    after = (await client.get("/api/v1/audit", params={"category": "client_certificates"})).json()
    assert len(after["items"]) == len(before["items"])


# --- the window the status was computed against ----------------------------------------------


@pytest.fixture
async def generous(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """An Apchi whose expiry window is wide enough to catch a year-long certificate."""
    app = create_app(settings.model_copy(update={"certificate_expiring_within_days": 400}))
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def test_a_certificate_reports_the_window_its_status_was_computed_against(
    client: AsyncClient,
) -> None:
    """Without it, `expiring` is a verdict with no stated threshold: an interface can say
    *expiring* but not *expiring within 30 days*."""
    body = (await _upload(client, "finance", bundle())).json()

    assert body["status"] == "valid"
    assert body["expiring_within_days"] == 30, "the default"


async def test_the_same_certificate_reads_differently_under_a_wider_window(
    client: AsyncClient, generous: AsyncClient
) -> None:
    """The recomputation made visible, and the hazard the CHANGELOG already warned about:
    `status` is derived from `not_after` on **every read** against this setting, so an Admin
    widening the window moves certificates into `expiring` with nothing about them having
    changed. Reporting the window next to the status is what lets an interface explain that
    rather than appear to contradict itself."""
    archive = bundle(valid_for_days=365)

    narrow = (await _upload(client, "finance", archive)).json()
    wide = (await _upload(generous, "finance", archive)).json()

    assert narrow["not_after"] == wide["not_after"], "the same certificate"
    assert narrow["status"] == "valid"
    assert narrow["expiring_within_days"] == 30
    assert wide["status"] == "expiring"
    assert wide["expiring_within_days"] == 400
