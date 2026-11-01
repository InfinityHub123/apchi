"""§16's ninth precondition: Trino must accept the scheme `trino_url` names (#188).

Every authentication type Trino supports requires TLS, and with an authenticator enabled
alongside HTTPS Trino refuses plain HTTP from every client unless
`http-server.authentication.allow-insecure-over-http` is true. So a Cluster that
authenticates its callers does not answer an Apchi configured for `http://` at all -- and
the symptom without this check is every query in the Apply timing out with nothing to read,
which is §16's own criterion for a precondition being worth having.

The answer lives in the coordinator's `config.properties`, which is the Admin's file (§15),
so it is read rather than inferred.
"""

import pytest

from app.config import Settings
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from tests.conftest import FakeKubernetes, healthy_pod_spec

#: What a coordinator that authenticates its callers by certificate has in
#: `config.properties`. `allow-insecure-over-http` is absent, which is its default and the
#: setting that closes the plaintext port.
CERTIFICATE_AUTH = (
    "http-server.https.enabled=true\n"
    "http-server.https.port=8443\n"
    "http-server.authentication.type=CERTIFICATE\n"
)


def _config(fake_kubernetes: FakeKubernetes, content: str) -> None:
    fake_kubernetes.config_maps["trino-config"]["coordinator-config.properties"] = content


async def test_plaintext_against_an_authenticating_coordinator_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The case #188 was filed for. Apchi's default `trino_url` is http, so this is what an
    Admin gets when they point a stock Apchi at a production cluster."""
    _config(fake_kubernetes, CERTIFICATE_AUTH)

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)

    problem = str(raised.value)
    assert "http-server.authentication.type=CERTIFICATE" in problem
    assert "allow-insecure-over-http" in problem
    assert repr(settings.trino_url) in problem


async def test_an_https_url_with_a_certificate_satisfies_it(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    _config(fake_kubernetes, CERTIFICATE_AUTH)
    configured = settings.model_copy(
        update={
            "trino_url": "https://trino:8443",
            "trino_auth_certificate_file": "/etc/apchi/trino-tls/tls.crt",
            "trino_auth_key_file": "/etc/apchi/trino-tls/tls.key",
        }
    )

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), configured)


async def test_certificate_authentication_without_a_certificate_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Reaching the coordinator is not the same as being let in. Trino demands a certificate
    from every client under this authenticator, Apchi included."""
    _config(fake_kubernetes, CERTIFICATE_AUTH)
    configured = settings.model_copy(update={"trino_url": "https://trino:8443"})

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), configured)

    assert "trino_auth_certificate_file is unset" in str(raised.value)


async def test_an_authenticator_that_is_not_certificate_needs_no_certificate(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """OAuth2 or JWT authenticate a browser or a bearer token, not the transport. Apchi
    reaching the coordinator over TLS is all this check can speak to there."""
    _config(
        fake_kubernetes,
        "http-server.https.enabled=true\nhttp-server.authentication.type=OAUTH2\n",
    )
    configured = settings.model_copy(update={"trino_url": "https://trino:8443"})

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), configured)


async def test_plaintext_is_accepted_when_the_admin_has_allowed_it(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Trino calls this not recommended and Apchi has no opinion about that: the property
    says the plaintext port answers, which is the only question here."""
    _config(
        fake_kubernetes,
        CERTIFICATE_AUTH + "http-server.authentication.allow-insecure-over-http=true\n",
    )

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_an_authenticator_without_https_is_not_this_checks_problem(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Trino's rule is conjunctive -- HTTP closes when an authenticator is enabled
    *alongside HTTPS*. An authenticator without TLS is a coordinator that refuses to start,
    which Verification reports and this would only mislabel."""
    _config(fake_kubernetes, "http-server.authentication.type=CERTIFICATE\n")

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_an_unauthenticated_cluster_on_https_is_accepted_over_plaintext(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """HTTPS alone does not close the plaintext port; only an authenticator does. An Apchi
    on the pod network talking http to such a cluster works, and refusing it would be
    Apchi inventing a security policy for somebody else's deployment."""
    _config(fake_kubernetes, "http-server.https.enabled=true\n")

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_an_https_url_against_a_coordinator_with_no_tls_property_is_allowed(
    settings: Settings, fake_kubernetes: FakeKubernetes, caplog: pytest.LogCaptureFixture
) -> None:
    """Logged and not refused. TLS terminated in front of Trino -- a sidecar, a mesh -- is a
    real deployment in which this property is absent and Apchi's https URL is still right,
    so refusing would reject a working Cluster. §16 refuses only what it knows is broken."""
    _config(fake_kubernetes, "http-server.http.port=8080\n")
    configured = settings.model_copy(update={"trino_url": "https://trino:8443"})

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), configured)

    assert "terminated in front of Trino" in caplog.text


async def test_a_config_properties_apchi_cannot_read_yields_no_opinion(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The one precondition that can hold having missed something, and deliberately: a
    deployment keeping its configuration somewhere Apchi cannot see gets no guess."""
    # Already the default: the shared fixture mounts config.properties but puts no content
    # behind it, which is the same position as a ConfigMap Apchi's Role cannot read.
    assert "coordinator-config.properties" not in fake_kubernetes.config_maps["trino-config"]

    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_adoption_does_not_call_this_a_deployment_change(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Adoption turns every other precondition failure into a cutover change, because §16
    is otherwise the list of what the *deployment* must provide. This one compares the
    deployment against Apchi's own configuration, so there can be nothing in the Trino
    manifest to fix -- and labelling it `cutover` would send an Admin looking for it.

    The same class of bug as #182's, where a message matched the catalog Secret's name by
    accident and arrived carrying an ordering it did not have.
    """
    from app.pipeline.discovery import discover

    _config(fake_kubernetes, CERTIFICATE_AUTH)

    discovery = await discover(fake_kubernetes, settings)

    own = [p for p in discovery.every_problem if p.kind == "apchi_configuration"]
    assert len(own) == 1
    assert "trino_url" in own[0].detail
    assert "must come after the seed" not in own[0].detail
    assert not [p for p in discovery.every_problem if p.kind == "cutover"]
