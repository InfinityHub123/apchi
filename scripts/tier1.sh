#!/usr/bin/env bash
# Run the tier 1 suite as several pytest processes instead of one.
#
# Not a speed optimisation. As one process the suite degrades until tests hit the 300s
# per-test limit: 5519s and 18 timeouts for the same tests that pass in about three minutes
# split three ways, on the same machine, with no assertion ever failing. Measured several
# times, including on a machine that was idle at the start and had 9 GB free at the end, so
# it is not contention. The mechanism is still unknown and #69 holds the evidence.
#
# CI is unaffected -- it runs tier 1 as one process and is green every time -- so this is a
# local workaround and deliberately not wired into the workflow. Nothing here is a
# diagnosis: it avoids whatever a single pytest process accumulates across ~40 files rather
# than explaining it.
#
#   scripts/tier1.sh            three chunks
#   scripts/tier1.sh 5          five chunks
#   scripts/tier1.sh 3 -x       chunks, with the rest passed through to pytest
set -euo pipefail

cd "$(dirname "$0")/.."

chunks=${1:-3}
if ! [[ $chunks =~ ^[1-9][0-9]*$ ]]; then
    echo "usage: scripts/tier1.sh [chunks] [pytest args...]" >&2
    exit 2
fi
shift || true

# Computed from the directory rather than listed, the way the tier 2 shards are: a new test
# file is covered without anyone remembering to add it.
mapfile -t files < <(ls tests/tier1/test_*.py)
if [[ ${#files[@]} -eq 0 ]]; then
    echo "no tier 1 test files found" >&2
    exit 1
fi
if [[ $chunks -gt ${#files[@]} ]]; then
    chunks=${#files[@]}
fi

# Every chunk runs even when an earlier one fails, and the exit code is the worst of them.
# A partial result is the thing to avoid here: the point of splitting is to see the whole
# suite, and stopping at the first failing chunk would hide the files after it.
failed=()
for ((chunk = 0; chunk < chunks; chunk++)); do
    this=()
    for ((i = chunk; i < ${#files[@]}; i += chunks)); do
        this+=("${files[i]}")
    done
    echo
    echo "=== chunk $((chunk + 1))/$chunks -- ${#this[@]} files ==="
    if ! uv run pytest "${this[@]}" -q --timeout=300 --timeout-method=signal "$@"; then
        failed+=("$((chunk + 1))")
    fi
done

echo
if [[ ${#failed[@]} -gt 0 ]]; then
    echo "chunks that failed: ${failed[*]}"
    exit 1
fi
echo "all $chunks chunks passed"
