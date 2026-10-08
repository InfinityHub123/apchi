"""Rendering the Permissions Section to the file Trino reads.

Only Apchi may create or drop Catalogs, and this is the file that enforces it.
`FileBasedSystemAccessControl.checkCanCreateCatalog` and `checkCanDropCatalog` gate on
the **owner** access mode, so the restriction is a `catalogs` rules block: Apchi's
identity gets `owner`, everyone else gets `all`. See section 7.1.

Operator-managed grants are not here yet. What this file already carries is the block Apchi
owns and nobody may edit, and the whole file is generated -- there is no hand-written part
to preserve when the grants arrive.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.sections.base import Resources
from app.sections.permissions.model import GrantWrite, key_of

RULES_KEY = "rules.json"

#: Trino's whole set, in its own order. An unknown one fails startup.
_EVERY_PRIVILEGE = ("SELECT", "INSERT", "UPDATE", "DELETE", "OWNERSHIP", "GRANT_SELECT")

#: The directory the Admin mounts the Secret at, and the file inside it. Apchi writes the
#: Secret and never touches the mount: it has to be a whole volume, because a subPath mount
#: never receives updates and Trino would read these rules once and never again (section 16).
MOUNT_DIR = "/etc/trino/access-control"
MOUNT_PATH = f"{MOUNT_DIR}/{RULES_KEY}"

#: Where Trino reads which access control to use, and how often to re-read its rules. The
#: Admin's file, not Apchi's -- but Apchi depends on one property in it, so it checks.
PROPERTIES_PATH = "/etc/trino/access-control.properties"

#: Without this, Trino reads the rules once at startup and never again -- and Apchi would
#: report success for permissions the Cluster will never read. Section 13.4.
REFRESH_PERIOD = "security.refresh-period"

#: What the validation probe is given, so that it *reads* the rules rather than merely
#: holding them: a file Trino was not told to read is a file Trino never rejects. The
#: Cluster's copy of this file is the Admin's, and Apchi checks one property in it (§16);
#: the probe is Apchi's own, so Apchi writes the whole thing.
PROBE_PROPERTIES = (
    f"access-control.name=file\n{REFRESH_PERIOD}=1s\nsecurity.config-file={MOUNT_PATH}\n"
)


def reserved_table_rule(trino_user: str, verification_catalog: str) -> dict[str, Any]:
    """Apchi's own read access, and the reason the whole tables block is safe to write.

    A `tables` block makes every table it does not match **denied, to everyone** -- verified
    against a running coordinator, where `apchi` was refused a table it plainly owned the
    catalog of. Verification's smoke query reads a table, the running-query count reads
    another, and §13.5's group read-back reads a third; all three live in the verification
    catalog. Without this rule the first Apply that wrote a grant would pass Verification
    only by luck of the catch-all below, and the day that catch-all is removed Apchi would
    lose the connection it needs to recover.

    The same trap §8 added the reserved identity for, and the mapping twin of §13.3's
    reserved rule.
    """
    return {
        "user": f"^{re.escape(trino_user)}$",
        "catalog": f"^{re.escape(verification_catalog)}$",
        "privileges": ["SELECT"],
    }


def query_rules(trino_user: str, identities: list[str]) -> list[dict[str, Any]]:
    """Who may run, see and kill queries.

    By default any authenticated End User can view and kill any query -- the Web UI
    documentation says so outright -- and query text routinely contains data. This block
    closes that, and every part of it was checked against a running coordinator because the
    format's behaviour is not what it looks like:

    * **execute for everyone, last.** The block is all-or-nothing: once a `queries` section
      exists, anything unmatched is denied, `execute` included. Without this rule the Cluster
      stops serving queries at all -- which is why §13.4 insists an Operator can see it.
    * **Nobody has to be granted sight of their own queries.** Trino gives a user their own
      rows whatever the rules say: with only the catch-all above, alice saw her query and not
      bob's. The rules are what stops her seeing *his*.
    * **Killing your own query is not implicit, and cannot be expressed generically.** There
      is no back-reference from `queryOwner` to the requesting user, and a rule carrying a
      `queryOwner` may not carry `execute` at all ("A valid query rule cannot combine an
      queryOwner condition with access mode 'execute'"). So it is a rule per identity, for
      the identities Apchi knows -- the ones named in a grant.
    * **Apchi's own rule comes first**, granting view over everyone's queries. Not vanity:
      the running-query count Review shows before a Rollout reads
      `system.runtime.queries`, and Trino filters those rows by who may view them. Without
      this Apchi would count its own queries and report that a Rollout destroys nothing.
    """
    rules: list[dict[str, Any]] = [
        {"user": f"^{re.escape(trino_user)}$", "allow": ["execute", "view", "kill"]}
    ]
    rules.extend(
        {
            "user": f"^{re.escape(identity)}$",
            "queryOwner": f"^{re.escape(identity)}$",
            "allow": ["view", "kill"],
        }
        for identity in identities
        if identity != trino_user
    )
    rules.append({"allow": ["execute"]})
    return rules


#: The one procedure Apchi grants, and the only one it took away that anybody noticed. A
#: file-based access control denies procedure execution unless a rule allows it, so Apchi's
#: own file removed `CALL system.runtime.kill_query(...)` from every End User the day it was
#: installed -- including from an owner killing their own query. Verified by contrast: with
#: no access control at all, that same kill succeeds.
_KILL_QUERY = {
    "catalog": "^system$",
    "schema": "^runtime$",
    "procedure": "^kill_query$",
    "privileges": ["EXECUTE"],
}


def procedure_rules() -> list[dict[str, Any]]:
    """Who may execute which procedure.

    Everyone, and only this one. Granting the procedure does not decide *whose* query may be
    killed -- the queries block above still does that, verified against a running
    coordinator: with this rule in place, one End User was still refused another's query and
    allowed their own. So this restores a capability without widening an authority.

    Nothing else is granted. Trino's own runtime schema has neighbours and every connector
    brings procedures of its own, and handing out execute on all of them would be granting
    what nobody has asked for or tested.
    """
    return [_KILL_QUERY]


def _grant_rule(grant: dict[str, Any]) -> dict[str, Any]:
    """One grant as Trino wants it.

    Every name is anchored. Trino matches these as regular expressions, so an unanchored
    `finance` would also grant on `finance_archive` -- a grant nobody wrote and nobody
    would notice.
    """
    rule: dict[str, Any] = {
        "user": f"^{re.escape(grant['identity'])}$",
        "catalog": f"^{re.escape(grant['catalog'])}$",
    }
    if grant.get("schema") is not None:
        rule["schema"] = f"^{re.escape(grant['schema'])}$"
    if grant.get("table") is not None:
        rule["table"] = f"^{re.escape(grant['table'])}$"
    rule["privileges"] = list(grant["privileges"])
    return rule


#: Where preserved rules sit, and the rule is the same in every block: **beneath the
#: Operator's own and above the catch-all**.
#:
#: First match wins, so position is the configuration. Beneath the Operator's grants, because
#: the grants are what the Cluster is migrating *to* and a subject matching both should
#: resolve to the destination rather than the origin -- §13.3 made the same choice for
#: certificate mapping patterns. Above the catch-all, because a catch-all that swallowed them
#: would make preserving them pointless the moment enforcement is switched on, which is
#: exactly when they are load-bearing.
def _with_preserved_queries(
    generated: list[dict[str, Any]], preserved_rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Queries, where the catch-all is the last rule and must stay last.

    The block is all-or-nothing: once a `queries` section exists, anything unmatched is
    denied, `execute` included. So the rule letting everyone execute is what keeps the
    Cluster serving, and nothing may be appended after it.
    """
    if not preserved_rules:
        return generated
    return [*generated[:-1], *preserved_rules, generated[-1]]


def render_rules(
    trino_user: str,
    grants: Resources | None = None,
    verification_catalog: str = "system",
    enforced: bool = False,
    preserved: Mapping[str, list[dict[str, Any]]] | None = None,
) -> str:
    """The whole file: what Apchi owns, then the Operator's grants, then the catch-alls.

    The catch-all rule is **mandatory**, not a convenience. `canAccessCatalog` returns
    false when no rule matches, so omitting it denies every End User access to every
    catalog -- the same trap as the `queries` block in §13.4. First match wins, so
    Apchi's rule has to come first: `owner` implies `all`, and the catch-all would
    otherwise swallow it.

    The identity is anchored. Trino matches these patterns against the whole username,
    so a bare `apchi` already cannot match `notapchi`, but anchoring says so out loud --
    the cost of being wrong here is handing catalog DDL to anyone whose username
    happens to contain Apchi's.
    """
    staged = grants or {}
    kept = dict(preserved or {})
    rules: dict[str, Any] = {
        "catalogs": [
            {"user": f"^{re.escape(trino_user)}$", "allow": "owner"},
            *kept.pop("catalogs", []),
            {"allow": "all"},
        ],
        "tables": [
            reserved_table_rule(trino_user, verification_catalog),
            *(_grant_rule(staged[key]) for key in sorted(staged)),
            *kept.pop("tables", []),
            # Everything the grants do not name, for everyone -- which is what the Cluster
            # already did before Apchi wrote a tables block at all. Writing grants must not
            # quietly become an act of revocation, so this stays until an Admin decides the
            # grants are complete and takes it away (§14). Removing it is the whole of what
            # "enforced" means: there is nothing else to change, because Apchi's own access
            # is granted by its own rule above rather than by this one.
            *([] if enforced else [{"privileges": list(_EVERY_PRIVILEGE)}]),
        ],
        "procedures": [*procedure_rules(), *kept.pop("procedures", [])],
        "queries": _with_preserved_queries(
            query_rules(trino_user, sorted({staged[key]["identity"] for key in staged})),
            kept.pop("queries", []),
        ),
    }
    # Blocks Apchi does not model at all -- impersonation, system_information, schemas,
    # functions, authorization. Nothing here generates them, so they pass through whole.
    rules.update({block: list(preserved_rules) for block, preserved_rules in sorted(kept.items())})
    return json.dumps(rules, indent=2) + "\n"


class Unreadable(Exception):
    """A file this module cannot read. Translated by the Section, so the generator keeps
    knowing nothing about the pipeline."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


#: What `_grant_rule` writes, and nothing else. A rule carrying anything beyond these is not a
#: grant Apchi's model can hold, whatever it looks like.
_GRANT_KEYS = frozenset({"user", "catalog", "schema", "table", "privileges"})

_ANCHORED = re.compile(r"^\^(.*)\$$")


def _unanchor(pattern: str) -> str | None:
    """The literal name behind an anchored, escaped pattern, or None if it is a real regex.

    Apchi writes `^` + `re.escape(name)` + `$` for every name it anchors, so the inverse is
    exact for anything Apchi wrote. A hand-written rule matching several catalogs with one
    pattern is not a grant Apchi can hold -- it is one identity's access to a *set*, and the
    model has a name where that set would go -- so it is preserved rather than approximated.
    """
    matched = _ANCHORED.match(pattern)
    if matched is None:
        return None
    literal = matched.group(1)
    return literal if re.escape(re.sub(r"\\(.)", r"\1", literal)) == literal else None


def _as_grant(rule: Mapping[str, Any]) -> dict[str, Any] | None:
    """One `tables` rule as a Grant, or None when Apchi's model cannot hold it."""
    if set(rule) - _GRANT_KEYS or not {"user", "catalog", "privileges"} <= set(rule):
        return None
    names: dict[str, Any] = {}
    for field in ("user", "catalog", "schema", "table"):
        if field not in rule:
            continue
        if not isinstance(rule[field], str):
            return None
        literal = _unanchor(rule[field])
        if literal is None:
            return None
        names[field] = re.sub(r"\\(.)", r"\1", literal)
    privileges = rule["privileges"]
    if not isinstance(privileges, list) or not all(isinstance(p, str) for p in privileges):
        return None
    return {
        "identity": names["user"],
        "catalog": names["catalog"],
        "schema": names.get("schema"),
        "table": names.get("table"),
        "privileges": privileges,
    }


def parse_rules(
    path: str, content: str, trino_user: str, verification_catalog: str = "system"
) -> tuple[Resources, dict[str, list[dict[str, Any]]], bool]:
    """The inverse of `render_rules`, as far as an inverse exists.

    Returns the Operator's grants, the rules Apchi cannot express, and whether the Cluster is
    enforcing.

    Apchi's model is **deliberately smaller than the file** (§13.4), and no amount of parser
    work changes that: a `tables` rule is a grant only if it names one identity and one
    catalog literally and carries nothing else. A rule matching a set of catalogs with one
    regex is one identity's access to a set, and the model has a name where that set would
    go. Those are preserved rather than approximated, because an approximated grant is a
    permission change nobody asked for.

    Apchi's own rules are recognised and dropped. On a Cluster that already ran Apchi they
    would otherwise be imported as Operator grants and generated a second time. They are
    matched against what this module would generate for the same identity rather than by
    shape, so the two cannot drift.
    """
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise Unreadable(path, f"not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise Unreadable(path, "the file is not a JSON object")

    #: The rule whose presence *is* "not enforcing": everything the grants do not name, for
    #: everyone. Removing it is the whole of what enforcement means (§14), so its presence is
    #: the only thing that has to be read to know -- and reading it wrong would switch a
    #: Cluster's enforcement on or off behind an Admin's back.
    catch_all = {"privileges": list(_EVERY_PRIVILEGE)}

    grants: Resources = {}
    preserved: dict[str, list[dict[str, Any]]] = {}
    enforced = True

    def rules_in(block: str) -> list[dict[str, Any]]:
        rules = document.get(block, [])
        if not isinstance(rules, list):
            raise Unreadable(path, f"the {block!r} block is not a list of rules")
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise Unreadable(path, f"{block}[{index}] is not an object")
        return rules

    def keep(block: str, rule: dict[str, Any]) -> None:
        preserved.setdefault(block, []).append(rule)

    # `tables` first, because the identities in the grants decide which `queries` rules Apchi
    # would have generated -- there is one kill rule per identity Apchi knows about, and
    # recognising them needs the grants to exist already.
    reserved = reserved_table_rule(trino_user, verification_catalog)
    for rule in rules_in("tables"):
        if rule == reserved:
            continue
        if rule == catch_all:
            enforced = False
            continue
        grant = _as_grant(rule)
        if grant is None:
            keep("tables", rule)
            continue
        # Validated through the model rather than trusted, so a rule Trino accepts and
        # Apchi's grants do not -- a privilege it has no name for -- is preserved instead of
        # staged and rejected later.
        try:
            write = GrantWrite.model_validate(grant)
        except ValidationError:
            keep("tables", rule)
            continue
        grants[key_of(write)] = write.model_dump(mode="json", by_alias=True)

    owned = {
        "catalogs": [{"user": f"^{re.escape(trino_user)}$", "allow": "owner"}, {"allow": "all"}],
        "procedures": procedure_rules(),
        "queries": query_rules(
            trino_user, sorted({grant["identity"] for grant in grants.values()})
        ),
    }
    for block in sorted(set(document) - {"tables"}):
        for rule in rules_in(block):
            if rule in owned.get(block, []):
                continue
            keep(block, rule)

    return grants, preserved, enforced
