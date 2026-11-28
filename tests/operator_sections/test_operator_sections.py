"""Every Section an Operator configures, on a real system, checked from where end users stand.

Tier 2 proves each Section against `deploy/trino-dev`: plain HTTP, the name a client sends,
and data sources that are never really connected to. This is the chart's default shape
instead -- HTTPS, sign-in by certificate -- with people holding certificates from one CA and a
PostgreSQL that accepts nothing but a client certificate, and every claim is made by
connecting as one of them. It is how the Validation probe was found to lack the certificates
its Catalogs present (#314): nothing else here connects a Catalog to a real database.

The tests are one story and run in order: configured and applied first, then what each
person can do, then changing and recovering. `tests/operator_sections/setup.sh` builds the
system, and $WORK is where it left the certificates.
"""

import contextlib
import os
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import trino.auth
import trino.dbapi
import trino.exceptions

WORK = Path(os.environ.get("WORK", "/tmp/operator-sections"))
NAMESPACE = "trino"

pytestmark = pytest.mark.skipif(
    not (WORK / "ca.crt").exists(), reason="run tests/operator_sections/setup.sh first"
)

#: How long a rules file may take to reach Trino: the kubelet's projection -- measured at up to
#: 88s on kind -- and then the chart's 30s security.refresh-period.
PROPAGATION = 240.0

#: A query that runs for minutes and needs no table, so it stays RUNNING or QUEUED while a test
#: looks at it, and every identity may issue it.
LONG = (
    "SELECT count(*) FROM UNNEST(sequence(1, 10000)) a(x) CROSS JOIN UNNEST(sequence(1, 10000)) "
    "b(y) CROSS JOIN UNNEST(sequence(1, 10000)) c(z) WHERE x + y + z > 0"
)

PG_URL = (
    f"jdbc:postgresql://postgres.{NAMESPACE}.svc:5432/finance"
    "?sslmode=verify-full&sslrootcert=/etc/pki/tls/certs/ca-bundle.crt"
)
MAPPING = {"pattern": "CN=([^,]+), OU=[^,]+, O=Acme", "user": "$1", "case": "lower"}


def _forward(target: str, ports: str) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "port-forward", target, ports],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return process


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    forward = _forward("svc/apchi", "18000:8000")
    try:
        with httpx.Client(base_url="http://127.0.0.1:18000/api/v1", timeout=60) as client:
            yield client
    finally:
        forward.terminate()


def _ok(response: httpx.Response) -> Any:
    assert response.status_code < 300, (response.status_code, response.text)
    return response.json() if response.content else None


def _apply(api: httpx.Client, path: str = "/applies") -> dict[str, Any]:
    record = _ok(api.post(path))
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        record = _ok(api.get(f"/applies/{record['id']}"))
        if record["stage"] in ("succeeded", "failed", "incident"):
            return dict(record)
        time.sleep(2)
    raise AssertionError("the Apply never settled")


def _validation(api: httpx.Client) -> dict[str, Any]:
    record = _ok(api.post("/validations"))
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        record = _ok(api.get(f"/validations/{record['id']}"))
        if record["outcome"] != "running":
            return dict(record)
        time.sleep(2)
    raise AssertionError("the Validation never finished")


class _Https:
    """Trino's HTTPS port, re-attached per use: a coordinator restart ends a port-forward."""

    def __enter__(self) -> "_Https":
        self._forward = _forward("svc/trino", "18443:8443")
        return self

    def __exit__(self, *_: object) -> None:
        self._forward.terminate()

    @staticmethod
    def connect(person: str, session_user: str | None = None) -> trino.dbapi.Connection:
        return trino.dbapi.connect(
            host="https://127.0.0.1:18443",
            user=session_user or person,
            auth=trino.auth.CertificateAuthentication(
                str(WORK / f"{person}.crt"), str(WORK / f"{person}.key")
            ),
            verify=str(WORK / "ca.crt"),
        )

    def query(self, person: str, sql: str, session_user: str | None = None) -> list[list[Any]]:
        cursor = self.connect(person, session_user).cursor()
        cursor.execute(sql)
        return [list(row) for row in cursor.fetchall()]

    def attempt(self, person: str, sql: str, session_user: str | None = None) -> tuple[bool, str]:
        try:
            return True, str(self.query(person, sql, session_user))
        except Exception as exc:  # what Trino said, whatever it was, is the evidence
            return False, str(getattr(exc, "message", exc))

    def rest(self, person: str, method: str, path: str) -> httpx.Response:
        context = ssl.create_default_context(cafile=str(WORK / "ca.crt"))
        context.load_cert_chain(str(WORK / f"{person}.crt"), str(WORK / f"{person}.key"))
        return httpx.request(
            method,
            f"https://127.0.0.1:18443{path}",
            headers={"X-Trino-User": person},
            verify=context,
            timeout=30,
        )


def _until(check: Callable[[], bool], what: str, within: float = PROPAGATION) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(5)
    raise AssertionError(f"{what} did not happen within {within:.0f}s")


def _as_apchi(*statements: str) -> list[list[Any]]:
    """Through the in-namespace HTTP port, by name, as Apchi itself reaches Trino. The rows of
    the first statement."""
    forward = _forward("svc/trino", "18080:8080")
    try:
        rows: list[list[Any]] = []
        for index, sql in enumerate(statements):
            cursor = trino.dbapi.connect(host="http://127.0.0.1:18080", user="apchi").cursor()
            cursor.execute(sql)
            fetched = [list(row) for row in cursor.fetchall()]
            rows = fetched if index == 0 else rows
        return rows
    finally:
        forward.terminate()


def _bundle(person: str) -> bytes:
    import io
    import zipfile

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("client.crt", (WORK / f"{person}.crt").read_bytes())
        zipped.writestr("client.key", (WORK / f"{person}.key").read_bytes())
    return archive.getvalue()


def test_every_section_validates_and_applies_in_one_go(api: httpx.Client) -> None:
    """A certificate, three Catalogs -- one presenting it to a database that requires one --
    a mapping pattern, two grants and a hierarchy of resource groups: one Validation with
    nothing to warn about, one Apply with one restart. Then the Admin closes the Cluster."""
    certificate = {"archive": ("pg_reader.zip", _bundle("trino_reader"), "application/zip")}
    _ok(api.post("/certificates", data={"name": "pg_reader"}, files=certificate))
    for catalog in (
        {
            "name": "finance",
            "connector": "postgresql",
            "certificate": "pg_reader",
            "properties": {"connection-url": PG_URL, "connection-user": "trino_reader"},
        },
        {"name": "sales", "connector": "tpch", "properties": {}},
        {"name": "scratch", "connector": "memory", "properties": {}},
    ):
        _ok(api.post("/catalogs", json=catalog))
    _ok(api.put("/certificate-mapping", json=MAPPING))
    for grant in (
        {"identity": "alice", "catalog": "finance", "schema": "public", "privileges": ["SELECT"]},
        {"identity": "bob", "catalog": "sales", "schema": "tiny", "privileges": ["SELECT"]},
    ):
        _ok(api.post("/permissions", json=grant))
    for group in (
        {"path": "global", "hard_concurrency_limit": 20, "max_queued": 100},
        {"path": "global.finance", "hard_concurrency_limit": 2, "max_queued": 10},
        {"path": "global.analytics", "hard_concurrency_limit": 1, "max_queued": 1},
        {"path": "global.other", "hard_concurrency_limit": 5, "max_queued": 10},
    ):
        _ok(api.post("/resource-groups", json=group))
    selectors = [
        {"user": "alice", "group": "global.finance"},
        {"user": "bob", "group": "global.analytics"},
        {"group": "global.other"},
    ]
    _ok(api.put("/resource-groups/selectors", json={"selectors": selectors}))

    verdict = _validation(api)
    assert verdict["outcome"] == "passed", verdict
    assert verdict["warnings"] == [], "the probe reached every Catalog, the database included"

    record = _apply(api)
    assert record["stage"] == "succeeded", record
    assert record["snapshot"] == 1
    assert [e["stage"] for e in record["history"]].count("rolling_out") == 1

    _ok(api.put("/admin/permissions/enforcement", json={"enforced": True}))
    assert _apply(api, "/admin/applies")["stage"] == "succeeded"


def test_a_certificate_becomes_the_identity_the_mapping_derives() -> None:
    with _Https() as https:
        for person in ("alice", "bob", "carol"):
            assert https.query(person, "SELECT current_user") == [[person]]
        # CA-signed, outside the pattern: the catch-all keeps the subject as presented.
        dave = "CN=dave, O=Other"
        assert https.query("dave", "SELECT current_user", dave) == [[dave]]
        # A certificate no trusted CA signed is refused before any query runs.
        trusted, said = https.attempt("mallory", "SELECT 1")
        assert not trusted and "SSL" in said, said
        # And a mapped identity cannot pass for another.
        assert not https.attempt("alice", "SELECT 1", "bob")[0]


#: What each person may do once the Cluster is closed: the grants and nothing else.
EXPECTED = [
    ("alice", "SELECT count(*) FROM finance.public.ledger", True),
    ("alice", "SELECT count(*) FROM sales.tiny.nation", False),
    ("bob", "SELECT count(*) FROM sales.tiny.nation", True),
    ("bob", "SELECT count(*) FROM sales.sf1.nation", False),
    ("bob", "SELECT count(*) FROM finance.public.ledger", False),
    ("carol", "SELECT count(*) FROM finance.public.ledger", False),
    ("carol", "SELECT count(*) FROM sales.tiny.nation", False),
    ("carol", "SELECT count(*) FROM system.runtime.nodes", False),
    ("carol", "SELECT 1", True),
]


def test_each_identity_reaches_what_it_was_granted_and_nothing_else() -> None:
    with _Https() as https:
        _until(
            lambda: not https.attempt("carol", "SELECT count(*) FROM sales.tiny.nation")[0],
            "enforcement reaching Trino",
        )
        wrong = {}
        for person, sql, allowed in EXPECTED:
            got, said = https.attempt(person, sql)
            if got is not allowed:
                wrong[(person, sql)] = said
        assert wrong == {}, wrong
        # The ledger came from PostgreSQL over verify-full, presenting the uploaded certificate.
        assert https.query("alice", "SELECT count(*) FROM finance.public.ledger") == [[3]]
        for sql in ("CREATE CATALOG rogue USING memory", "DROP CATALOG sales"):
            allowed, said = https.attempt("bob", sql)
            assert not allowed and "Access Denied" in said, said


def test_resource_groups_route_by_identity_and_hold_their_limits() -> None:
    """Alice's group runs two at once, Bob's one with one queued, so the third is refused."""
    refusals: list[str] = []

    def submit(person: str) -> None:
        try:
            _Https.connect(person).cursor().execute(LONG)
        except trino.exceptions.TrinoQueryError as exc:  # killed at the end, or refused
            refusals.append(str(getattr(exc, "message", exc)))

    with _Https():
        threads = [
            threading.Thread(target=submit, args=(p,)) for p in ("bob",) * 3 + ("alice",) * 3
        ]
        for thread in threads:
            thread.start()
            time.sleep(0.7)
        time.sleep(6)
        live = _as_apchi(
            'SELECT "user", state, resource_group_id, query_id FROM system.runtime.queries '
            "WHERE state IN ('RUNNING', 'QUEUED') AND \"user\" <> 'apchi'"
        )
        _as_apchi(
            *(
                f"CALL system.runtime.kill_query(query_id => '{row[3]}', message => 'done')"
                for row in live
            )
        )
        for thread in threads:
            thread.join(timeout=60)

    def count(person: str, state: str) -> int:
        return sum(1 for row in live if row[0] == person and row[1] == state)

    assert {tuple(row[2]) for row in live if row[0] == "alice"} == {("global", "finance")}
    assert {tuple(row[2]) for row in live if row[0] == "bob"} == {("global", "analytics")}
    assert (count("alice", "RUNNING"), count("alice", "QUEUED")) == (2, 1), live
    assert (count("bob", "RUNNING"), count("bob", "QUEUED")) == (1, 1), live
    assert any('Too many queued queries for "global.analytics"' in r for r in refusals), refusals


def test_a_query_is_seen_and_killed_by_its_owner_alone() -> None:
    finished = threading.Event()

    def run() -> None:
        # The kill below is how it is meant to end.
        with contextlib.suppress(trino.exceptions.TrinoQueryError):
            _Https.connect("alice").cursor().execute(LONG)
        finished.set()

    with _Https() as https:
        threading.Thread(target=run).start()
        hers: list[str] = []

        def running() -> bool:
            hers[:] = [
                q["queryId"]
                for q in https.rest("alice", "GET", "/v1/query").json()
                if q["state"] in ("RUNNING", "QUEUED")
            ]
            return bool(hers)

        _until(running, "alice's query starting", within=60)
        for person in ("bob", "carol"):
            seen = {q["session"]["user"] for q in https.rest(person, "GET", "/v1/query").json()}
            assert "alice" not in seen, (person, seen)
        assert https.rest("bob", "DELETE", f"/v1/query/{hers[0]}").status_code == 403
        assert https.rest("alice", "DELETE", f"/v1/query/{hers[0]}").status_code == 204
        assert finished.wait(60), "her query did not end when she killed it"


def test_validation_names_a_catalog_its_database_refuses(api: httpx.Client) -> None:
    """The certificate is the right one, the user is not hers: PostgreSQL says so, and the
    warning carries its words rather than a missing file on the probe (#314)."""
    wrong = {
        "name": "finance_wrong",
        "connector": "postgresql",
        "certificate": "pg_reader",
        "properties": {"connection-url": PG_URL, "connection-user": "postgres"},
    }
    _ok(api.post("/catalogs", json=wrong))
    try:
        verdict = _validation(api)
    finally:
        _ok(api.delete("/catalogs/finance_wrong"))

    reasons = {w["resource"]: w["reason"] for w in verdict["warnings"]}
    assert set(reasons) == {"finance_wrong"}, reasons
    assert "certificate authentication failed" in reasons["finance_wrong"]


def test_a_revoked_grant_is_lost_without_a_restart_and_a_revert_gives_it_back(
    api: httpx.Client,
) -> None:
    sql = "SELECT count(*) FROM sales.tiny.nation"
    _ok(api.delete("/permissions/bob:sales:tiny:*"))
    record = _apply(api)
    assert record["stage"] == "succeeded" and record["rolled_out"] is False, record

    with _Https() as https:
        _until(lambda: not https.attempt("bob", sql)[0], "bob losing sales")
        effect = _ok(api.post("/permissions/revert", json={"snapshot": 1}))
        assert "Snapshot 1" in effect["summary"]
        assert _apply(api)["stage"] == "succeeded"
        _until(lambda: https.attempt("bob", sql)[0], "bob getting sales back")


def test_a_renewed_certificate_reaches_the_database_without_a_restart(
    api: httpx.Client,
) -> None:
    """Renewal keeps the name, so the Catalog that presents it is not touched, and the next
    connection presents the new one."""
    renewed = WORK / "trino_reader.renewed.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-new",
            "-key",
            "trino_reader.key",
            "-out",
            "renew.csr",
            "-subj",
            "/CN=trino_reader",
        ],
        cwd=WORK,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            "renew.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            renewed.name,
            "-days",
            "3",
            "-extfile",
            "trino_reader.ext",
        ],
        cwd=WORK,
        check=True,
        capture_output=True,
    )
    (WORK / "trino_reader.crt").write_bytes(renewed.read_bytes())
    before = _ok(api.get("/certificates/pg_reader"))
    certificate = {"archive": ("pg_reader.zip", _bundle("trino_reader"), "application/zip")}
    after = _ok(api.post("/certificates", data={"name": "pg_reader"}, files=certificate))
    assert after["not_after"] != before["not_after"]

    record = _apply(api)
    assert record["stage"] == "succeeded" and record["rolled_out"] is False, record
    with _Https() as https:
        assert https.query("alice", "SELECT count(*) FROM finance.public.ledger") == [[3]]


def test_a_new_mapping_moves_identities_and_a_revert_moves_them_back(api: httpx.Client) -> None:
    """Grants follow the identity the mapping derives, so renaming everyone takes Alice's
    grant away from her until the pattern is reverted."""
    ledger = "SELECT count(*) FROM finance.public.ledger"
    _ok(api.put("/certificate-mapping", json={**MAPPING, "user": "$1_renamed"}))
    record = _apply(api)
    assert record["stage"] == "succeeded" and record["rolled_out"] is True, record
    with _Https() as https:
        assert https.query("alice", "SELECT current_user", "alice_renamed") == [["alice_renamed"]]
        assert not https.attempt("alice", ledger, "alice_renamed")[0]

    _ok(api.post("/certificate-mapping/revert", json={"snapshot": 1}))
    record = _apply(api)
    assert record["stage"] == "succeeded" and record["rolled_out"] is True, record
    with _Https() as https:
        assert https.query("alice", "SELECT current_user") == [["alice"]]
        assert https.query("alice", ledger) == [[3]]
