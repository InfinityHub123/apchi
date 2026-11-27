"""How callers sign in to Trino, decided by the chart: certificate authentication by default,
plain HTTP and the name a client sends when the chart says `insecure` (#295).

Under `certificate`, HTTPS authenticates the clients a Route or Ingress brings, and the HTTP
port takes a user name -- the port Apchi and the workers use, which the NetworkPolicy keeps to
the namespace. Tier 1 runs the `insecure` shape against a plain-HTTP Trino, so the certificate
shape is named here; that a real coordinator refuses a client without a certificate on HTTPS, and
answers Apchi on HTTP, is the Argo CD job's, which installs the chart's default.
"""

import pytest

from app.config import Settings
from app.sections.server_configuration import (
    CONFIG_PROPERTIES,
    ServerConfiguration,
    probe_properties,
    render_coordinator,
)

TYPE = "http-server.authentication.type"
OVER_HTTP = "http-server.authentication.allow-insecure-over-http"


def _certificate() -> Settings:
    return Settings(_env_file=None, trino_authentication_type="certificate")  # type: ignore[call-arg]


def _coordinator(properties: dict[str, str]) -> ServerConfiguration:
    return ServerConfiguration.model_validate({"coordinator": {"config.properties": properties}})


def test_the_default_is_certificate_authentication() -> None:
    assert Settings(_env_file=None).trino_authentication_type == "certificate"  # type: ignore[call-arg]


def test_with_nothing_set_https_authenticates_by_certificate_and_http_by_name() -> None:
    rendered = render_coordinator(ServerConfiguration(), _certificate())[CONFIG_PROPERTIES]

    assert f"{TYPE}=CERTIFICATE\n" in rendered
    assert f"{OVER_HTTP}=true\n" in rendered


@pytest.mark.parametrize("value", ["PASSWORD,CERTIFICATE", "PASSWORD"])
def test_an_admin_sets_whichever_methods_https_offers(value: str) -> None:
    """Any list, CERTIFICATE or not: Apchi signs in over HTTP, so none locks it out."""
    rendered = render_coordinator(_coordinator({TYPE: value}), _certificate())[CONFIG_PROPERTIES]

    assert f"{TYPE}={value}\n" in rendered
    assert rendered.count(TYPE) == 1


def test_the_http_override_is_not_the_admins() -> None:
    """Apchi reaches Trino through it: without it, Trino refuses plain HTTP from every client
    once an authenticator is on, and every query Apchi issues goes unanswered."""
    with pytest.raises(ValueError, match=f"{OVER_HTTP} is wiring"):
        _coordinator({OVER_HTTP: "false"})


def test_the_insecure_shape_renders_no_authenticator() -> None:
    insecure = Settings(_env_file=None, trino_authentication_type="insecure")  # type: ignore[call-arg]

    rendered = render_coordinator(ServerConfiguration(), insecure)[CONFIG_PROPERTIES]

    assert TYPE not in rendered
    assert OVER_HTTP not in rendered


def test_the_probe_is_given_no_authenticator() -> None:
    """It serves plain HTTP with no shared secret, where Trino will not start one: an Admin who
    adds PASSWORD would otherwise fail every Validation."""
    configuration = _coordinator(
        {
            TYPE: "PASSWORD,CERTIFICATE",
            "http-server.authentication.password.user-mapping.pattern": "(.*)",
            "query.max-execution-time": "2h",
        }
    )

    probed = probe_properties(configuration)["config.properties"]

    assert probed == {"query.max-execution-time": "2h"}


@pytest.mark.parametrize("role", ["coordinator", "worker"])
def test_the_shared_secret_is_not_the_admins(role: str) -> None:
    with pytest.raises(ValueError, match="internal-communication.shared-secret is wiring"):
        ServerConfiguration.model_validate(
            {role: {"config.properties": {"internal-communication.shared-secret": "mine"}}}
        )
