"""Apchi refuses to start on an `APCHI_` variable it does not read.

pydantic-settings matches a variable to a setting by name and passes over any other, and its
`extra="forbid"` does not change that. A real deployment set two variables that did not
exist, ran on the defaults they were meant to replace, and spent a day on a MongoDB timeout
that never named a setting.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings, refuse_unrecognised


def test_an_unrecognised_variable_stops_apchi_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCHI_MONGO_CERT_FILE", "/etc/apchi/tls.crt")

    with pytest.raises(ValidationError, match="APCHI_MONGO_CERT_FILE"):
        Settings()


def test_the_refusal_suggests_the_setting_it_is_closest_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="did you mean APCHI_LOG_LEVEL"):
        refuse_unrecognised({"APCHI_LOG_LEVL": "debug"})


def test_every_unrecognised_variable_is_named_at_once() -> None:
    with pytest.raises(ValueError) as raised:
        refuse_unrecognised({"APCHI_ONE": "1", "APCHI_TWO": "2"})

    assert "APCHI_ONE" in str(raised.value)
    assert "APCHI_TWO" in str(raised.value)


def test_a_real_setting_is_recognised_in_any_case() -> None:
    """pydantic-settings matches names case-insensitively, so the check has to as well."""
    refuse_unrecognised({"APCHI_LOG_LEVEL": "info", "apchi_debug": "true"})


def test_other_variables_are_none_of_apchis_business() -> None:
    refuse_unrecognised({"PATH": "/usr/bin", "HOME": "/", "KUBERNETES_SERVICE_HOST": "10.0.0.1"})


@pytest.mark.parametrize(
    "name",
    [
        # Measured: what Kubernetes injects into every pod in a namespace where the chart's
        # Services are named apchi and apchi-mongodb.
        "APCHI_PORT",
        "APCHI_SERVICE_HOST",
        "APCHI_SERVICE_PORT",
        "APCHI_SERVICE_PORT_HTTP",
        "APCHI_PORT_8000_TCP",
        "APCHI_PORT_8000_TCP_ADDR",
        "APCHI_PORT_8000_TCP_PORT",
        "APCHI_PORT_8000_TCP_PROTO",
        "APCHI_MONGODB_PORT",
        "APCHI_MONGODB_SERVICE_HOST",
        "APCHI_MONGODB_SERVICE_PORT_MONGODB",
        "APCHI_MONGODB_PORT_27017_TCP_ADDR",
    ],
)
def test_kubernetes_service_links_are_not_mistaken_for_settings(name: str) -> None:
    """They share the prefix because the chart's Service is called apchi, not because anyone
    misspelt a setting -- and refusing them would crash every install."""
    refuse_unrecognised({name: "10.96.0.1"})
