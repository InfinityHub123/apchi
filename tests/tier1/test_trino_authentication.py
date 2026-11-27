"""How callers sign in to Trino, decided by the chart's trino.https.enabled: HTTPS beside an
in-namespace HTTP port by default, plain HTTP and the name a client sends without it (#295).

Beside HTTPS, HTTPS authenticates the clients a Route or Ingress brings -- by certificate unless
an Admin sets other methods -- and the HTTP port takes a user name: the port Apchi and the
workers use, which the NetworkPolicy keeps to the namespace. Tier 1 runs against a plain-HTTP
Trino, so the HTTPS shape is named here; that a real coordinator refuses a client without a
certificate on HTTPS, starts with an Admin's PASSWORD alone, and answers Apchi on HTTP, is the
Argo CD job's, which installs the chart's default.
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
CERTIFICATE_MAPPING = "http-server.authentication.certificate.user-mapping.file"
INSECURE_MAPPING = "http-server.authentication.insecure.user-mapping.file"


def _https() -> Settings:
    return Settings(_env_file=None, trino_https_port=8443)  # type: ignore[call-arg]


def _coordinator(properties: dict[str, str]) -> ServerConfiguration:
    return ServerConfiguration.model_validate({"coordinator": {"config.properties": properties}})


def test_with_nothing_set_https_authenticates_by_certificate_and_http_by_name() -> None:
    rendered = render_coordinator(ServerConfiguration(), _https())[CONFIG_PROPERTIES]

    assert f"{TYPE}=CERTIFICATE\n" in rendered
    assert f"{OVER_HTTP}=true\n" in rendered
    assert CERTIFICATE_MAPPING in rendered
    assert INSECURE_MAPPING not in rendered


@pytest.mark.parametrize("value", ["PASSWORD,CERTIFICATE", "password, certificate"])
def test_the_certificate_mapping_is_wired_while_certificate_is_a_method(value: str) -> None:
    rendered = render_coordinator(_coordinator({TYPE: value}), _https())[CONFIG_PROPERTIES]

    assert f"{TYPE}={value}\n" in rendered
    assert rendered.count(f"{TYPE}=") == 1
    assert CERTIFICATE_MAPPING in rendered


@pytest.mark.parametrize("value", ["PASSWORD", "OAUTH2", "PASSWORD,OAUTH2"])
def test_without_certificate_nothing_reads_the_certificate_mapping(value: str) -> None:
    """Trino refuses to start on a property no configured authenticator reads -- measured on
    483 with PASSWORD alone: "Configuration property ... was not used"."""
    rendered = render_coordinator(_coordinator({TYPE: value}), _https())[CONFIG_PROPERTIES]

    assert f"{TYPE}={value}\n" in rendered
    assert "user-mapping" not in rendered
    assert f"{OVER_HTTP}=true\n" in rendered, "Apchi still signs in over HTTP"


@pytest.mark.parametrize("value", ["", " ", ",", " , "])
def test_a_list_of_no_methods_is_refused(value: str) -> None:
    """Trino reads it as no authenticator, and HTTPS would then take any name it is sent."""
    with pytest.raises(ValueError, match="names no method"):
        _coordinator({TYPE: value})


def test_the_http_override_is_not_the_admins() -> None:
    """Apchi reaches Trino through it: without it, Trino refuses plain HTTP from every client
    once an authenticator is on, and every query Apchi issues goes unanswered."""
    with pytest.raises(ValueError, match=f"{OVER_HTTP} is wiring"):
        _coordinator({OVER_HTTP: "false"})


@pytest.mark.parametrize("mapping", [CERTIFICATE_MAPPING, INSECURE_MAPPING])
def test_which_authenticator_reads_the_mapping_is_not_the_admins(mapping: str) -> None:
    with pytest.raises(ValueError, match=f"{mapping} is wiring"):
        _coordinator({mapping: "/etc/trino/elsewhere.json"})


def test_without_https_no_authenticator_is_rendered() -> None:
    plain = Settings(_env_file=None)  # type: ignore[call-arg]

    rendered = render_coordinator(ServerConfiguration(), plain)[CONFIG_PROPERTIES]

    assert TYPE not in rendered
    assert OVER_HTTP not in rendered
    assert f"{INSECURE_MAPPING}=" in rendered


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
