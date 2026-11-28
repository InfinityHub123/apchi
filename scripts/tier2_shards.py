"""Which tier 2 files a CI shard runs, balanced by what each file was measured to cost.

    python scripts/tier2_shards.py <shard> <shards>

Prints the files of one shard, space separated. Every tier 2 file runs in exactly one shard,
including a file nobody has measured: it counts as the median, so a new file runs without
anyone remembering to list it.

By measured seconds rather than by position in the directory listing, because positions put
the five most expensive modules in one shard, which then took 23 minutes while the others
took 7 to 10 (#300). The weights are `tests/tier2/durations.json`: each file's seconds from a
CI run's `--durations` output. Refresh them when a module's cost changes a lot.
"""

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIER2 = ROOT / "tests" / "tier2"


def shards(count: int) -> list[list[str]]:
    measured: dict[str, float] = json.loads((TIER2 / "durations.json").read_text())
    files = sorted(path.name for path in TIER2.glob("test_*.py"))
    unknown = statistics.median(measured.values()) if measured else 1.0
    cost = {name: measured.get(name, unknown) for name in files}
    # Longest first, each onto the lightest shard so far: within 4/3 of the best split, and
    # deterministic, so a shard means the same files on every runner.
    loads: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(count)]
    for name in sorted(files, key=lambda name: (-cost[name], name)):
        lightest = min(range(count), key=lambda index: (loads[index][0], index))
        total, members = loads[lightest]
        loads[lightest] = (total + cost[name], [*members, name])
    return [sorted(members) for _, members in loads]


def main() -> None:
    shard, count = int(sys.argv[1]), int(sys.argv[2])
    if not 1 <= shard <= count:
        raise SystemExit(f"shard {shard} is not one of 1..{count}")
    print(" ".join(f"tests/tier2/{name}" for name in shards(count)[shard - 1]))


if __name__ == "__main__":
    main()
