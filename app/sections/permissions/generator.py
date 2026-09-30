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
from typing import Any

from app.sections.base import Resources

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


def render_rules(
    trino_user: str,
    grants: Resources | None = None,
    verification_catalog: str = "system",
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
    rules: dict[str, Any] = {
        "catalogs": [
            {"user": f"^{re.escape(trino_user)}$", "allow": "owner"},
            {"allow": "all"},
        ],
        "tables": [
            reserved_table_rule(trino_user, verification_catalog),
            *(_grant_rule(staged[key]) for key in sorted(staged)),
            # Everything the grants do not name, for everyone -- which is what the Cluster
            # already did before Apchi wrote a tables block at all. Writing grants must not
            # quietly become an act of revocation: narrowing this is a decision of its own,
            # and it is not this file's to make.
            {"privileges": list(_EVERY_PRIVILEGE)},
        ],
        "queries": query_rules(trino_user, sorted({staged[key]["identity"] for key in staged})),
    }
    return json.dumps(rules, indent=2) + "\n"
