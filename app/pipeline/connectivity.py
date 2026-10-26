"""Whether the Catalogs in a Candidate can actually reach what they name.

A rule that belongs to no Section's own validation, which is why it sits here beside
`references.py` rather than inside the Catalogs Section: it is not about whether the
configuration is well formed but about whether the world it describes is there.

**`CREATE CATALOG` succeeding proves nothing about reachability.** Verified against Trino
483 -- a catalog pointed at a host that does not resolve is created without complaint, and
fails only when something reads from it:

    CREATE CATALOG pg USING postgresql WITH (
      "connection-url" = 'jdbc:postgresql://nosuchhost.invalid:5432/db', ...)
      -> OK

§6 has always said as much ("some connectors initialise lazily, so a wrong JDBC URL can
pass startup validation and only fail at query time"). This closes it.

**Advisory by default.** §6 also warns, in the same breath, that the probe pod must reach
the data sources a catalog references "or catalog validation fails for reasons unrelated to
the configuration". Both are true at once, and they pull opposite ways: the check is worth
running and its verdict is not reliable enough to refuse an Apply on. So it reports, and
`catalog_connectivity_blocks_apply` is there for a deployment where the probe and the
coordinator are known to share a network.
"""

import asyncio
import logging
from typing import Final

from app.adapters.trino import Trino
from app.sections.base import Resources, ValidationFailure
from app.sections.catalogs import SECTION as CATALOGS

logger = logging.getLogger(__name__)

#: How many catalogs are asked at once.
#:
#: Bounded rather than unbounded: each query holds a thread for as long as the connection
#: attempt takes, which on a blackholed host is the whole timeout. Twenty catalogs asked at
#: once on a bad day is twenty threads doing nothing for ten seconds. Four is enough to keep
#: a realistic Candidate under the timeout without that.
_AT_ONCE: Final = 4


async def unreachable(probe: Trino, catalogs: Resources) -> list[ValidationFailure]:
    """One entry per Catalog the probe could not read schemas from.

    Collected rather than raised, because the caller decides what they are: this returns
    the same shape whether they end up warnings or failures, and only the setting says
    which. Trino's own words are passed through -- "Error listing schemas for catalog pg:
    The connection attempt failed." is more use to whoever has to fix it than anything
    Apchi could write about it.
    """
    if not catalogs:
        return []

    gate = asyncio.Semaphore(_AT_ONCE)

    async def ask(name: str) -> ValidationFailure | None:
        async with gate:
            try:
                await probe.schemas_of(name)
            except Exception as exc:
                return ValidationFailure(
                    section=CATALOGS,
                    resource=name,
                    reason=f"The validation coordinator could not reach this Catalog: {exc}",
                )
            return None

    results = await asyncio.gather(*(ask(name) for name in sorted(catalogs)))
    found = [result for result in results if result is not None]
    logger.info(
        "catalog reachability checked",
        extra={"catalogs": len(catalogs), "unreachable": len(found)},
    )
    return found
