"""Permissions discovery: grants where Apchi's model reaches, Admin values where it does not.

The lossy Section. Apchi's grants say an identity may do these things to this catalog, schema
or table; Trino's file says a great deal more. A Cluster onboarded years into its life has
rules with no grant to become, and no amount of parser work changes that -- the model is
deliberately smaller than the file (§13.4).

So the question this file answers is not "can Apchi parse it" but "does anybody lose access".
The answer has to be no, and the test that matters is the last one: regenerating from what was
discovered grants the same decisions the Cluster already made.
"""

import json

import pytest

from app.config import Settings
from app.pipeline.discovery import discover
from app.sections.permissions.generator import (
    MOUNT_PATH,
    parse_rules,
    procedure_rules,
    query_rules,
    render_rules,
    reserved_table_rule,
)
from app.sections.permissions.model import GrantWrite, key_of
from tests.conftest import FakeKubernetes

APCHI = "apchi"


def _grants(*writes: GrantWrite) -> dict:
    return {key_of(write): write.model_dump(mode="json", by_alias=True) for write in writes}


def _rules(fake: FakeKubernetes, settings: Settings, content: str) -> None:
    """Put a rules file on the Cluster where the healthy fixture mounts it."""
    fake.secrets[settings.access_control_secret_name] = {"rules.json": content}


# --- the round trip, which is the whole point ---------------------------------------


@pytest.mark.parametrize("enforced", [False, True])
def test_grants_preserved_rules_and_enforcement_all_round_trip(enforced: bool) -> None:
    """Both directions, because the file direction is the one that matters at runtime: the
    first Apply rewrites this file from what Apchi holds, so anything lost on the way in is
    deleted from the Cluster on the way out."""
    grants = _grants(
        GrantWrite(identity="analyst", catalog="sales", privileges=["SELECT"]),
        GrantWrite.model_validate(
            {
                "identity": "etl",
                "catalog": "warehouse",
                "schema": "staging",
                "privileges": ["SELECT", "INSERT"],
            }
        ),
    )
    preserved = {
        "tables": [{"user": "^legacy$", "catalog": "^(a|b)$", "privileges": ["SELECT"]}],
        "impersonation": [{"original_user": "^svc$", "new_user": ".*"}],
    }

    rendered = render_rules(APCHI, grants, "system", enforced=enforced, preserved=preserved)
    back_grants, back_preserved, back_enforced = parse_rules(MOUNT_PATH, rendered, APCHI, "system")

    assert back_grants == grants
    assert back_preserved == preserved
    assert back_enforced is enforced
    assert (
        render_rules(APCHI, back_grants, "system", enforced=back_enforced, preserved=back_preserved)
        == rendered
    )


def test_a_hand_written_file_regenerates_the_same_decisions() -> None:
    """The acceptance criterion that matters most, and the reason preserving beats refusing:
    a file nobody wrote with Apchi in mind comes back granting what it granted.

    Every rule here is checked for individually rather than by comparing whole files, because
    Apchi reorders and adds its own -- what must survive is the access, not the layout.
    """
    theirs = json.dumps(
        {
            "catalogs": [
                {"user": "^reporting$", "allow": "read-only"},
                {"allow": "all"},
            ],
            "tables": [
                {"user": "^analyst$", "catalog": "^sales$", "privileges": ["SELECT"]},
                {"user": "^legacy$", "catalog": "^(a|b)$", "privileges": ["SELECT"]},
                {
                    "privileges": [
                        "SELECT",
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "OWNERSHIP",
                        "GRANT_SELECT",
                    ]
                },
            ],
            "impersonation": [{"original_user": "^svc_.*$", "new_user": ".*"}],
            "system_information": [{"user": "^ops$", "allow": ["read"]}],
        }
    )

    grants, preserved, enforced = parse_rules(MOUNT_PATH, theirs, APCHI, "system")
    regenerated = json.loads(
        render_rules(APCHI, grants, "system", enforced=enforced, preserved=preserved)
    )

    # The grant Apchi could express, now expressed as a grant.
    assert {"user": "^analyst$", "catalog": "^sales$", "privileges": ["SELECT"]} in (
        regenerated["tables"]
    )
    # The rule it could not, kept verbatim.
    assert {"user": "^legacy$", "catalog": "^(a|b)$", "privileges": ["SELECT"]} in (
        regenerated["tables"]
    )
    # The blocks Apchi does not model at all, kept whole.
    assert regenerated["impersonation"] == [{"original_user": "^svc_.*$", "new_user": ".*"}]
    assert regenerated["system_information"] == [{"user": "^ops$", "allow": ["read"]}]
    # Their own catalogs rule, kept, and still above the catch-all that makes it reachable.
    assert regenerated["catalogs"].index({"user": "^reporting$", "allow": "read-only"}) < (
        regenerated["catalogs"].index({"allow": "all"})
    )
    # Not enforcing, because their file had the catch-all -- adopting must not switch that on.
    assert enforced is False


# --- where preserved rules sit ------------------------------------------------------


def test_preserved_rules_sit_beneath_the_operators_grants_and_above_the_catch_all() -> None:
    """First match wins, so position is the configuration.

    Beneath the grants, because the grants are what the Cluster is migrating *to* and a
    subject matching both should resolve to the destination rather than the origin -- §13.3
    made the same choice for certificate mapping patterns. Above the catch-all, because a
    catch-all that swallowed them would make preserving them pointless the moment enforcement
    is switched on, which is exactly when they are load-bearing.
    """
    grants = _grants(GrantWrite(identity="analyst", catalog="sales", privileges=["SELECT"]))
    preserved = {"tables": [{"user": "^legacy$", "catalog": "^old$", "privileges": ["SELECT"]}]}

    tables = json.loads(render_rules(APCHI, grants, "system", preserved=preserved))["tables"]

    assert tables == [
        reserved_table_rule(APCHI, "system"),
        {"user": "^analyst$", "catalog": "^sales$", "privileges": ["SELECT"]},
        {"user": "^legacy$", "catalog": "^old$", "privileges": ["SELECT"]},
        tables[-1],
    ]


def test_nothing_is_ever_placed_after_the_queries_catch_all() -> None:
    """The block is all-or-nothing: once a `queries` section exists, anything unmatched is
    denied, `execute` included. So the rule letting everyone execute keeps the Cluster
    serving, and a preserved rule appended after it would stop queries entirely."""
    preserved = {"queries": [{"user": "^ops$", "allow": ["view"]}]}

    queries = json.loads(render_rules(APCHI, {}, "system", preserved=preserved))["queries"]

    assert queries[-1] == {"allow": ["execute"]}
    assert {"user": "^ops$", "allow": ["view"]} in queries[:-1]


# --- Apchi's own rules are not adopted ----------------------------------------------


def test_apchis_own_rules_are_not_imported_as_operator_grants() -> None:
    """On a Cluster that already ran Apchi they would otherwise become grants and then be
    generated a second time. Matched against what the generator would produce rather than by
    shape, so the two cannot drift."""
    generated = render_rules(APCHI, {}, "system")

    grants, preserved, enforced = parse_rules(MOUNT_PATH, generated, APCHI, "system")

    assert grants == {}
    assert preserved == {}
    assert enforced is False


def test_the_per_identity_kill_rules_apchi_generates_are_not_preserved() -> None:
    """These exist one per identity named in a grant, so recognising them needs the grants --
    which is why the parser reads `tables` before `queries`. Getting it wrong preserved
    Apchi's own rules as the Admin's, and they would then be generated twice."""
    grants = _grants(GrantWrite(identity="analyst", catalog="sales", privileges=["SELECT"]))
    generated = render_rules(APCHI, grants, "system")

    back_grants, preserved, _ = parse_rules(MOUNT_PATH, generated, APCHI, "system")

    assert back_grants == grants
    assert preserved == {}
    assert query_rules(APCHI, ["analyst"]) == json.loads(generated)["queries"]
    assert procedure_rules() == json.loads(generated)["procedures"]


# --- what Apchi's model cannot hold -------------------------------------------------


def test_a_rule_matching_a_set_of_catalogs_is_preserved_not_approximated() -> None:
    """It is one identity's access to a *set*, and the model has a name where that set would
    go. An approximated grant is a permission change nobody asked for."""
    theirs = json.dumps(
        {"tables": [{"user": "^analyst$", "catalog": "^sales_.*$", "privileges": ["SELECT"]}]}
    )

    grants, preserved, _ = parse_rules(MOUNT_PATH, theirs, APCHI, "system")

    assert grants == {}
    assert preserved["tables"] == [
        {"user": "^analyst$", "catalog": "^sales_.*$", "privileges": ["SELECT"]}
    ]


def test_a_privilege_apchi_has_no_name_for_is_preserved_rather_than_staged() -> None:
    """Staging it would mean a grant rejected later by the model, after an Operator had been
    told the Cluster was understood."""
    theirs = json.dumps(
        {"tables": [{"user": "^analyst$", "catalog": "^sales$", "privileges": ["SOMETHING_NEW"]}]}
    )

    grants, preserved, _ = parse_rules(MOUNT_PATH, theirs, APCHI, "system")

    assert grants == {}
    assert preserved["tables"][0]["privileges"] == ["SOMETHING_NEW"]


def test_an_unanchored_identity_is_preserved() -> None:
    """Apchi anchors every name it writes, so an unanchored one was not Apchi's and does not
    mean one identity -- `analyst` also matches `analyst_archive`."""
    theirs = json.dumps(
        {"tables": [{"user": "analyst", "catalog": "^sales$", "privileges": ["SELECT"]}]}
    )

    grants, preserved, _ = parse_rules(MOUNT_PATH, theirs, APCHI, "system")

    assert grants == {}
    assert preserved["tables"]


# --- enforcement --------------------------------------------------------------------


def test_enforcement_is_read_from_the_catch_all_rather_than_assumed() -> None:
    """Reading it wrong would switch a Cluster's enforcement on or off behind an Admin's
    back, which is the irreversible-feeling morning §14 put this decision with them for."""
    assert parse_rules(MOUNT_PATH, render_rules(APCHI, {}, enforced=False), APCHI)[2] is False
    assert parse_rules(MOUNT_PATH, render_rules(APCHI, {}, enforced=True), APCHI)[2] is True


# --- unreadable files ---------------------------------------------------------------


def test_a_rules_file_that_is_not_json_is_refused_with_its_path() -> None:
    from app.sections.permissions.generator import Unreadable

    with pytest.raises(Unreadable) as raised:
        parse_rules(MOUNT_PATH, "not json", APCHI)

    assert raised.value.path == MOUNT_PATH
    assert "not valid JSON" in raised.value.reason


def test_a_block_that_is_not_a_list_of_rules_is_refused() -> None:
    from app.sections.permissions.generator import Unreadable

    with pytest.raises(Unreadable, match="not a list of rules"):
        parse_rules(MOUNT_PATH, '{"tables": "everything"}', APCHI)


# --- through discovery --------------------------------------------------------------


async def test_discovery_reports_grants_as_resources_and_the_rest_as_admin_values(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The split has to be visible. Preserved rules nobody can see are worse than no
    adoption: an Operator would read the grants and not understand the access people have."""
    _rules(
        fake_kubernetes,
        settings,
        json.dumps(
            {
                "tables": [{"user": "^analyst$", "catalog": "^sales$", "privileges": ["SELECT"]}],
                "impersonation": [{"original_user": "^svc$", "new_user": ".*"}],
            }
        ),
    )

    found = next(
        section
        for section in (await discover(fake_kubernetes, settings)).sections
        if section.section == "permissions"
    )

    assert found.resources["analyst:sales:*:*"]["privileges"] == ["SELECT"]
    assert found.admin["preserved_access_control"]["impersonation"] == [
        {"original_user": "^svc$", "new_user": ".*"}
    ]


async def test_preserved_rules_do_not_make_a_discovery_incomplete(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """They are held, just not by the Candidate. Counting them as problems would make a
    successfully adopted Cluster look incomplete forever."""
    _rules(
        fake_kubernetes,
        settings,
        json.dumps({"impersonation": [{"original_user": "^svc$", "new_user": ".*"}]}),
    )

    discovery = await discover(fake_kubernetes, settings)
    found = next(s for s in discovery.sections if s.section == "permissions")

    assert found.problems == []
    assert discovery.admin_values["preserved_access_control"]["impersonation"]


async def test_enforcement_is_carried_through_discovery_as_an_admin_value(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    _rules(fake_kubernetes, settings, render_rules(settings.trino_user, {}, enforced=True))

    discovery = await discover(fake_kubernetes, settings)

    assert discovery.admin_values["enforce_permissions"] is True
