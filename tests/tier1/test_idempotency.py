"""A retry must not do the work twice (§19).

The assertion that matters is `test_a_retried_apply_starts_one_apply`, and it counts Applies
rather than reading a status. §19 names four endpoints, but `POST /applies` is the one with a
real defect behind it: it refuses while an Apply is in flight, so the open window is a retry
arriving after the first *finished* -- and a second Apply means a second rollout, a second
Snapshot, and running queries terminated (§7.4). Everything else here is about giving a
retrying client a truthful answer instead of a 409 it cannot interpret.

The other thing being defended is narrower and easier to lose: replaying a response means
storing it, and `POST /tokens` answers with a secret Apchi deliberately never keeps. The claim
still has to happen -- a retry must not mint a second credential -- while the body must not be
stored at all.
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.api.idempotency import HEADER
from app.auth.identity import Identity
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

CATALOGS = "/api/v1/catalogs"
APPLIES = "/api/v1/applies"
TOKENS = "/api/v1/tokens"
PROPERTIES = {"connection-url": "jdbc:postgresql://db.internal:5432/finance"}

JO = Identity(subject="jo", role="admin", via="password")
SAM = Identity(subject="sam", role="admin", via="password")


def _catalog(name: str = "finance_pg") -> dict[str, object]:
    return {"name": name, "connector": "postgresql", "properties": PROPERTIES}


async def _serving(
    settings: Settings, fake: FakeKubernetes, who: Identity
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    app.dependency_overrides[caller] = lambda: who
    app.state.kubernetes = fake
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
async def http(settings: Settings, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    async for client in _serving(settings, fake_kubernetes, JO):
        yield client


# --- the defect this exists for -----------------------------------------------------------


async def test_a_retried_apply_starts_one_apply(http: AsyncClient) -> None:
    """Counted, not inferred from a status.

    The dangerous window is a retry arriving after the first Apply *finished* -- the in-flight
    guard already covers the rest -- so this waits for the first to reach a terminal stage
    before retrying, which is exactly when a second one would be allowed to start.
    """
    await http.post(CATALOGS, json=_catalog())
    first = await http.post(APPLIES, json={}, headers={HEADER: "retry-me"})
    assert first.status_code == 202, first.text

    app = http._transport.app  # type: ignore[attr-defined]
    while (await app.state.apply_store.in_flight()) is not None:
        pass

    second = await http.post(APPLIES, json={}, headers={HEADER: "retry-me"})

    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"], "the retry started a different Apply"
    assert second.headers.get("Idempotent-Replay") == "true"
    assert len(await app.state.apply_store.list()) == 1, "a second Apply was started"


async def test_without_a_key_a_second_apply_is_a_second_apply(http: AsyncClient) -> None:
    """Opt-in, and this is why: two Applies in a row is a reasonable thing to want, and only
    the header can tell that from a retry."""
    await http.post(CATALOGS, json=_catalog())
    app = http._transport.app  # type: ignore[attr-defined]

    await http.post(APPLIES, json={})
    while (await app.state.apply_store.in_flight()) is not None:
        pass
    await http.post(APPLIES, json={})
    while (await app.state.apply_store.in_flight()) is not None:
        pass

    assert len(await app.state.apply_store.list()) == 2


# --- a retrying client gets a truthful answer ---------------------------------------------


async def test_a_retried_create_returns_the_original_response(http: AsyncClient) -> None:
    """Rather than the 409 it gets today, which is an error for work that succeeded and which
    a client cannot tell from somebody else taking the name."""
    first = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "create-once"})
    assert first.status_code == 201, first.text

    second = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "create-once"})

    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replay") == "true"
    assert len((await http.get(CATALOGS)).json()) == 1


async def test_a_replay_is_byte_for_byte(http: AsyncClient) -> None:
    """Rebuilt from the recorded bytes rather than re-serialised, so a client comparing
    responses -- or a content length -- is not surprised."""
    first = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "exact"})

    second = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "exact"})

    assert second.content == first.content


async def test_a_replayed_delete_keeps_its_empty_body(http: AsyncClient) -> None:
    """204 has no body, and a mechanism that assumed JSON would break on it."""
    await http.post(CATALOGS, json=_catalog())
    first = await http.delete(f"{CATALOGS}/finance_pg", headers={HEADER: "drop-it"})
    assert first.status_code == 204, first.text

    second = await http.delete(f"{CATALOGS}/finance_pg", headers={HEADER: "drop-it"})

    assert second.status_code == 204, second.text
    assert second.content == b""


# --- the refusals -------------------------------------------------------------------------


async def test_the_same_key_with_a_different_request_is_refused(http: AsyncClient) -> None:
    """The caller believes those two calls are the same one, and exactly one of them is
    wrong. Replaying would answer the wrong question confidently."""
    await http.post(CATALOGS, json=_catalog("first"), headers={HEADER: "shared"})

    response = await http.post(CATALOGS, json=_catalog("second"), headers={HEADER: "shared"})

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "idempotency_key_reused"
    assert "second" not in [c["name"] for c in (await http.get(CATALOGS)).json()]


async def test_a_retry_while_the_first_is_running_is_told_so(http: AsyncClient) -> None:
    """Told apart from a replay deliberately: a client that waits and retries collects the
    recorded response, where one told "already processed" would stop and never collect it."""
    app = http._transport.app  # type: ignore[attr-defined]
    first = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "in-flight"})
    assert first.status_code == 201, first.text
    # Put the claim back to how it looks mid-request, rather than reconstructing the
    # fingerprint by hand: the digest covers the exact bytes the client sent, and guessing how
    # httpx spaces its JSON would make this test about serialisation instead of about the
    # in-flight branch.
    await app.state.idempotency_store._collection.update_many({}, {"$set": {"status": None}})

    response = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "in-flight"})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "idempotent_request_in_flight"


async def test_a_failed_request_releases_its_key(http: AsyncClient) -> None:
    """A retry after a failure must be allowed to actually run. Replaying a failure would
    make the key a way to get permanently stuck."""
    bad = await http.post(
        CATALOGS,
        json={"name": "no_url", "connector": "postgresql", "properties": {}},
        headers={HEADER: "reusable"},
    )
    assert bad.status_code == 422, bad.text

    good = await http.post(CATALOGS, json=_catalog(), headers={HEADER: "reusable"})

    assert good.status_code == 201, good.text


# --- keys belong to their actor -----------------------------------------------------------


async def test_two_actors_using_one_key_do_not_collide(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Scoped by actor and key together. Colliding would be bad enough; showing one Operator
    the other's response would be worse."""
    async for jo in _serving(settings, fake_kubernetes, JO):
        async for sam in _serving(settings, fake_kubernetes, SAM):
            mine = await jo.post(CATALOGS, json=_catalog("mine"), headers={HEADER: "same"})
            theirs = await sam.post(CATALOGS, json=_catalog("theirs"), headers={HEADER: "same"})

            assert mine.status_code == 201, mine.text
            assert theirs.status_code == 201, theirs.text
            assert {c["name"] for c in (await jo.get(CATALOGS)).json()} == {"mine", "theirs"}


# --- the secret that must not be stored ---------------------------------------------------


async def test_a_retried_mint_does_not_mint_twice(http: AsyncClient) -> None:
    """The claim is the part that matters here: a retry must not leave two live credentials
    where the caller expected one."""
    first = await http.post(TOKENS, json={"name": "nightly"}, headers={HEADER: "mint-once"})
    assert first.status_code == 201, first.text

    second = await http.post(TOKENS, json={"name": "nightly"}, headers={HEADER: "mint-once"})

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "idempotent_secret_not_replayable"
    assert len((await http.get(TOKENS)).json()) == 1, "the retry minted a second token"


async def test_a_minted_secret_never_reaches_the_idempotency_collection(
    http: AsyncClient,
) -> None:
    """Apchi stores a digest of a token and never the value (#131). This collection must not
    become the one place a live token exists."""
    minted = await http.post(TOKENS, json={"name": "nightly"}, headers={HEADER: "no-secrets"})
    secret = minted.json()["secret"]

    app = http._transport.app  # type: ignore[attr-defined]
    rows = [row async for row in app.state.idempotency_store._collection.find()]

    assert rows, "nothing was recorded, so this asserts nothing"
    assert secret not in str(rows)
    assert all(row["body"] is None for row in rows)


async def test_the_refusal_says_what_to_do(http: AsyncClient) -> None:
    """ "Already processed" leaves somebody wondering whether they have a token. Saying the
    credential cannot be shown again, and that nothing ran twice, does not."""
    await http.post(TOKENS, json={"name": "nightly"}, headers={HEADER: "advice"})

    second = await http.post(TOKENS, json={"name": "nightly"}, headers={HEADER: "advice"})

    message = second.json()["message"]
    assert "twice" in message
    assert "revoke" in message


# --- the window ---------------------------------------------------------------------------


async def test_keys_are_bounded_by_a_ttl_index(http: AsyncClient) -> None:
    """A key is only interesting for as long as a retry might arrive. Without this the
    collection grows forever, which is what every other append-only collection here has a
    TTL or a purpose for."""
    app = http._transport.app  # type: ignore[attr-defined]
    # Directly, because indexing happens once per process (#69) and another test in this run
    # may already have been the one that did it -- leaving this to pass by doing nothing.
    await app.state.idempotency_store.ensure_indexes(timedelta(hours=24))

    indexes = await app.state.idempotency_store._collection.index_information()

    expiring = [spec for spec in indexes.values() if "expireAfterSeconds" in spec]
    assert len(expiring) == 1, indexes
