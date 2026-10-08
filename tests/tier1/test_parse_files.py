"""Every Section that can read its own file back, and what a round trip loses.

The parse is not what makes these trustworthy -- the **round trip** is, in both directions.
Model to file to model catches a parser that drops a field. File to model to file catches a
parser that drops something the model has no room for, which is the direction that matters
at runtime: Adoption imports a Cluster's configuration and the first Apply rewrites these
files, so anything lost on the way in is deleted from the Cluster on the way out.

Where a round trip genuinely cannot be exact, the test says so and says why, because a
documented loss an Operator is told about is a different thing from a silent one.
"""

import pytest

from app.config import Settings
from app.sections.base import ParseProblem, parses_files
from app.sections.certificate_mapping import RESOURCE as MAPPING
from app.sections.certificate_mapping.generator import MOUNT_PATH as MAPPING_PATH
from app.sections.certificate_mapping.model import CertificateMappingWrite
from app.sections.certificate_mapping.section import CertificateMappingSection
from app.sections.client_certificates.generator import certificate_path, key_path
from app.sections.client_certificates.model import describe
from app.sections.client_certificates.section import ClientCertificatesSection
from app.sections.event_listeners.generator import MOUNT_PATH as LISTENER_PATH
from app.sections.event_listeners.section import EventListenersSection
from app.sections.registry import REGISTERED
from app.sections.resource_groups import SELECTORS, SETTINGS
from app.sections.resource_groups.generator import MANAGER_FILE, MANAGER_PATH, RULES_PATH
from app.sections.resource_groups.model import (
    ResourceGroupSettings,
    ResourceGroupWrite,
    Selector,
)
from app.sections.resource_groups.section import ResourceGroupsSection
from tests.certificates import certificate_pem, key_pair, key_pem

SETTINGS_OBJ = Settings(_env_file=None)


def _rendered(section, desired):
    from app.sections.admin import AdminValues

    return section.render_files(desired, SETTINGS_OBJ, AdminValues())


# --- which Sections can parse at all ------------------------------------------------


def test_which_sections_can_read_their_files_back() -> None:
    """Permissions cannot yet (#89), and the type system says which rather than a stub
    returning nothing and looking like a Cluster with no configuration.

    Catalogs can read its files, but files are only one of its three sources -- the
    reconciliation against `system.metadata.catalogs` is the pipeline's, because a source
    that is not a file is not something a Section's parser can reach (#88)."""
    can = {section.name for section in REGISTERED if parses_files(section)}

    assert can == {
        "catalogs",
        "certificate_mapping",
        "client_certificates",
        "event_listeners",
        "resource_groups",
    }


# --- certificate mapping ------------------------------------------------------------


def test_an_empty_mapping_file_round_trips_to_no_pattern() -> None:
    """The file Apchi writes for an empty Section is the one Trino behaves as though it had
    anyway, so reading it back must produce no Operator pattern rather than a catch-all."""
    section = CertificateMappingSection()

    parsed = section.parse_files(_rendered(section, {}), SETTINGS_OBJ)

    assert parsed.resources == {}
    assert parsed.complete


@pytest.mark.parametrize(
    "pattern",
    [
        CertificateMappingWrite(pattern="CN=(.*?),.*"),
        CertificateMappingWrite(pattern="^(.*)@example[.]com$", user="$1", case="lower"),
        CertificateMappingWrite(pattern=r"CN=([a-z]+)\.clients\..*", user="svc_$1", case="upper"),
        # A legal pattern identical to the catch-all Apchi appends. Matching Apchi's own
        # rules by content rather than by position made this one vanish on the way back in,
        # which is the silent loss this whole file exists to catch.
        CertificateMappingWrite(pattern="(.*)"),
    ],
)
def test_a_mapping_pattern_round_trips(pattern: CertificateMappingWrite) -> None:
    section = CertificateMappingSection()
    desired = {MAPPING: pattern.model_dump(mode="json")}

    files = _rendered(section, desired)
    parsed = section.parse_files(files, SETTINGS_OBJ)

    assert parsed.resources == desired
    assert parsed.complete
    assert _rendered(section, parsed.resources) == files


def test_apchis_own_rules_are_not_adopted_as_an_operators_pattern() -> None:
    """On a Cluster that already ran Apchi, importing the reserved rule or the catch-all as
    configuration would mean generating each of them twice."""
    section = CertificateMappingSection()

    parsed = section.parse_files(_rendered(section, {}), SETTINGS_OBJ)

    assert parsed.resources == {}


def test_further_patterns_are_reported_not_dropped() -> None:
    """Apchi holds one pattern and a hand-written file may have many. The extras are what
    §13.3 keeps as Admin values, so they must survive as something, and silence is the one
    outcome that loses them."""
    section = CertificateMappingSection()
    rules = f"""{{
      "rules": [
        {{"pattern": "^{SETTINGS_OBJ.trino_user}$", "user": "{SETTINGS_OBJ.trino_user}"}},
        {{"pattern": "CN=(.*?),.*"}},
        {{"pattern": "^legacy_(.*)$", "user": "$1"}},
        {{"pattern": "(.*)"}}
      ]
    }}"""

    parsed = section.parse_files({MAPPING_PATH: rules}, SETTINGS_OBJ)

    assert parsed.resources == {MAPPING: {"pattern": "CN=(.*?),.*", "user": "$1", "case": "keep"}}
    assert [found.what for found in parsed.unaccounted] == ["rule 2 is a further pattern"]
    assert parsed.unaccounted[0].content == {"pattern": "^legacy_(.*)$", "user": "$1"}
    assert not parsed.complete


def test_a_rule_using_something_apchi_does_not_model_is_reported() -> None:
    """`allow` is Trino's and not Apchi's, because a single rule that denies is a Cluster
    nobody can authenticate to."""
    section = CertificateMappingSection()
    rules = '{"rules": [{"pattern": "CN=(.*)", "allow": false}]}'

    parsed = section.parse_files({MAPPING_PATH: rules}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert "allow" in parsed.unaccounted[0].what


def test_a_mapping_file_that_is_not_json_names_the_path_and_the_reason() -> None:
    section = CertificateMappingSection()

    with pytest.raises(ParseProblem) as raised:
        section.parse_files({MAPPING_PATH: "not json at all"}, SETTINGS_OBJ)

    assert raised.value.path == MAPPING_PATH
    assert "not valid JSON" in raised.value.reason


def test_a_mapping_file_with_no_rules_array_is_refused() -> None:
    section = CertificateMappingSection()

    with pytest.raises(ParseProblem, match="no 'rules' array"):
        section.parse_files({MAPPING_PATH: '{"something": []}'}, SETTINGS_OBJ)


def test_an_absent_mapping_file_is_not_an_error() -> None:
    """A Cluster being adopted may have an authenticator pointed at a path with nothing
    there yet."""
    assert CertificateMappingSection().parse_files({}, SETTINGS_OBJ).resources == {}


# --- resource groups ----------------------------------------------------------------


def test_a_resource_group_tree_round_trips() -> None:
    """The tree exists only in the file and the Candidate is flat, so this is the parser
    with real structure to invert -- including rebuilding `global.etl` out of an `etl`
    nested under a `global`."""
    section = ResourceGroupsSection()
    desired = {
        "global": ResourceGroupWrite(
            hard_concurrency_limit=100, soft_memory_limit="80%"
        ).model_dump(mode="json"),
        "global.etl": ResourceGroupWrite(
            hard_concurrency_limit=10, max_queued=50, scheduling_policy="fair"
        ).model_dump(mode="json"),
        "global.etl.nightly": ResourceGroupWrite(hard_concurrency_limit=2).model_dump(mode="json"),
        "adhoc": ResourceGroupWrite(hard_concurrency_limit=5).model_dump(mode="json"),
        SETTINGS: ResourceGroupSettings(cpu_quota_period="1h").model_dump(mode="json"),
        SELECTORS: [
            Selector(group="global.etl", source="airflow").model_dump(mode="json"),
            Selector(group="adhoc", client_tags=["interactive"]).model_dump(mode="json"),
        ],
    }

    files = _rendered(section, desired)
    parsed = section.parse_files(files, SETTINGS_OBJ)

    assert parsed.resources == desired
    assert parsed.complete
    assert _rendered(section, parsed.resources) == files


def test_selector_order_survives_because_it_is_the_configuration() -> None:
    """First match wins, so a parser that sorted them would change what the Cluster does."""
    section = ResourceGroupsSection()
    desired = {
        "a": ResourceGroupWrite(hard_concurrency_limit=1).model_dump(mode="json"),
        "b": ResourceGroupWrite(hard_concurrency_limit=1).model_dump(mode="json"),
        SELECTORS: [
            Selector(group="b").model_dump(mode="json"),
            Selector(group="a").model_dump(mode="json"),
        ],
    }

    parsed = section.parse_files(_rendered(section, desired), SETTINGS_OBJ)

    assert [selector["group"] for selector in parsed.resources[SELECTORS]] == ["b", "a"]


def test_a_group_setting_apchi_does_not_model_is_reported_against_its_group() -> None:
    section = ResourceGroupsSection()
    rules = """{
      "rootGroups": [
        {"name": "global", "hardConcurrencyLimit": 10, "schedulingWeight": 5,
         "somethingTrinoHasAndApchiDoesNot": true}
      ],
      "selectors": []
    }"""

    parsed = section.parse_files({RULES_PATH: rules}, SETTINGS_OBJ)

    assert parsed.resources["global"]["hard_concurrency_limit"] == 10
    assert "global" in parsed.unaccounted[0].what
    assert "somethingTrinoHasAndApchiDoesNot" in parsed.unaccounted[0].what


def test_a_selector_matching_on_something_apchi_does_not_model_is_reported() -> None:
    section = ResourceGroupsSection()
    rules = """{
      "rootGroups": [{"name": "g", "hardConcurrencyLimit": 1}],
      "selectors": [{"group": "g", "somethingElse": "x"}]
    }"""

    parsed = section.parse_files({RULES_PATH: rules}, SETTINGS_OBJ)

    assert parsed.resources[SELECTORS] == [Selector(group="g").model_dump(mode="json")]
    assert "somethingElse" in parsed.unaccounted[0].what


def test_a_group_without_a_name_is_refused_rather_than_guessed_at() -> None:
    section = ResourceGroupsSection()

    with pytest.raises(ParseProblem, match="no name"):
        section.parse_files(
            {RULES_PATH: '{"rootGroups": [{"hardConcurrencyLimit": 1}]}'}, SETTINGS_OBJ
        )


def test_a_group_trino_would_accept_and_apchi_would_not_is_refused() -> None:
    """A negative concurrency limit is not something to import and discover at Apply."""
    section = ResourceGroupsSection()

    with pytest.raises(ParseProblem, match="not valid"):
        section.parse_files(
            {RULES_PATH: '{"rootGroups": [{"name": "g", "hardConcurrencyLimit": -1}]}'},
            SETTINGS_OBJ,
        )


def test_no_resource_group_files_means_nothing_configured() -> None:
    parsed = ResourceGroupsSection().parse_files({}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert parsed.complete


def test_a_properties_file_with_no_rules_is_reported() -> None:
    """Trino refuses to start when a file it was told to read is missing, so this is a
    Cluster that is already broken and an Admin should hear about it."""
    parsed = ResourceGroupsSection().parse_files({MANAGER_PATH: MANAGER_FILE}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert "not there" in parsed.unaccounted[0].what


def test_a_properties_file_pointing_elsewhere_is_reported_not_followed() -> None:
    """Trino reads whatever config-file names, so the rules Apchi just read may not be the
    rules in force."""
    section = ResourceGroupsSection()
    files = {
        RULES_PATH: '{"rootGroups": [{"name": "g", "hardConcurrencyLimit": 1}], "selectors": []}',
        MANAGER_PATH: (
            "resource-groups.configuration-manager=file\n"
            "resource-groups.config-file=/etc/trino/somebody-elses.json\n"
        ),
    }

    parsed = section.parse_files(files, SETTINGS_OBJ)

    assert parsed.resources["g"]["hard_concurrency_limit"] == 1
    assert any("not the one Apchi writes" in found.what for found in parsed.unaccounted)


# --- event listeners ----------------------------------------------------------------


def test_an_event_listener_round_trips_except_for_its_name() -> None:
    """The name does not survive, and this is the test that says so.

    Trino's format has a type and properties and nowhere to put a name, so a listener an
    Operator called `audit` comes back called `http`. A real loss rather than a
    normalisation -- the alternative is inventing a name an Operator cannot find.
    """
    section = EventListenersSection()
    desired = {
        "audit": {
            "type": "http",
            "properties": {
                "http-event-listener.connect-ingest-uri": "http://collector:8080/events",
                "http-event-listener.log-completed": "true",
            },
        }
    }

    files = _rendered(section, desired)
    parsed = section.parse_files(files, SETTINGS_OBJ)

    assert parsed.resources == {"http": desired["audit"]}
    assert parsed.complete
    # The configuration is intact even though the name is not, which is what matters: the
    # file regenerates byte-for-byte from what came back.
    assert _rendered(section, parsed.resources) == files


def test_no_event_listener_file_means_no_listener() -> None:
    """The absence of the file is how this Section says "none configured" -- it is the only
    way to tell Trino there is no listener."""
    parsed = EventListenersSection().parse_files({}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert parsed.complete


def test_a_listener_file_with_no_plugin_named_is_refused() -> None:
    section = EventListenersSection()

    with pytest.raises(ParseProblem, match="event-listener.name"):
        section.parse_files({LISTENER_PATH: "some-property=value\n"}, SETTINGS_OBJ)


def test_a_listener_line_that_is_not_a_property_is_refused() -> None:
    section = EventListenersSection()

    with pytest.raises(ParseProblem, match="not key=value"):
        section.parse_files({LISTENER_PATH: "event-listener.name=http\nnonsense\n"}, SETTINGS_OBJ)


def test_a_comment_in_a_listener_file_is_reported_because_apchi_cannot_write_one() -> None:
    """Trino ignores comments, so this is not a problem -- but Apchi cannot write one back,
    so a round trip loses it and that is exactly what reporting is for."""
    section = EventListenersSection()

    parsed = section.parse_files(
        {LISTENER_PATH: "# why this listener exists\nevent-listener.name=http\n"}, SETTINGS_OBJ
    )

    assert parsed.resources == {"http": {"type": "http", "properties": {}}}
    assert "comment" in parsed.unaccounted[0].what


# --- client certificates ------------------------------------------------------------


def test_a_certificate_pair_round_trips() -> None:
    section = ClientCertificatesSection()
    certificate, key = key_pair()
    desired = {
        "finance": {
            "certificate": certificate_pem(certificate).decode(),
            "private_key": key_pem(key).decode(),
        }
    }

    files = _rendered(section, desired)
    parsed = section.parse_files(files, SETTINGS_OBJ)

    assert parsed.resources == desired
    assert parsed.complete
    assert _rendered(section, parsed.resources) == files


def test_a_parsed_certificates_metadata_comes_from_the_certificate() -> None:
    """Nothing about the CN, subject, issuer or expiry is stored, so a parsed certificate
    reports the same things an uploaded one does -- from the same bytes."""
    section = ClientCertificatesSection()
    certificate, key = key_pair(common_name="finance.clients.example.com")
    files = {
        certificate_path("finance"): certificate_pem(certificate).decode(),
        key_path("finance"): key_pem(key).decode(),
    }

    parsed = section.parse_files(files, SETTINGS_OBJ)
    described = describe("finance", parsed.resources["finance"]["certificate"], 30)

    assert described.common_name == "finance.clients.example.com"
    assert described.status == "valid"
    assert described.not_after == certificate.not_valid_after_utc


def test_a_certificate_with_no_key_is_reported_not_staged() -> None:
    """Half a pair cannot be presented to a data source, so staging it would be staging
    something that silently does not work."""
    section = ClientCertificatesSection()
    certificate, _ = key_pair()

    parsed = section.parse_files(
        {certificate_path("lonely"): certificate_pem(certificate).decode()}, SETTINGS_OBJ
    )

    assert parsed.resources == {}
    assert "no private key" in parsed.unaccounted[0].what


def test_a_key_with_no_certificate_is_reported_without_its_content() -> None:
    """Unaccounted entries get carried around and eventually shown to somebody, and private
    key material does not belong in any of that."""
    section = ClientCertificatesSection()
    _, key = key_pair()

    parsed = section.parse_files({key_path("orphan"): key_pem(key).decode()}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert "no certificate" in parsed.unaccounted[0].what
    assert parsed.unaccounted[0].content is None


def test_a_file_in_the_certificate_directory_apchi_does_not_own_is_reported() -> None:
    section = ClientCertificatesSection()

    parsed = section.parse_files({"/etc/trino/certs/truststore.jks": "binary"}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert "does not own" in parsed.unaccounted[0].what


def test_an_empty_certificate_directory_is_no_certificates() -> None:
    """An empty Secret mounts as an empty directory, so there is nothing to distinguish."""
    parsed = ClientCertificatesSection().parse_files({}, SETTINGS_OBJ)

    assert parsed.resources == {}
    assert parsed.complete
