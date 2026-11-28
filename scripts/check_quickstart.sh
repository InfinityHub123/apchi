#!/usr/bin/env bash
# Runs ./scripts/quickstart.sh exactly as docs/getting-started.md says to, then takes one
# catalog through an Apply and finds it in Trino, then --delete. CI runs it on a fresh kind
# cluster, so the page a new user follows first cannot rot unnoticed (#299, #300).
#
#   ./scripts/check_quickstart.sh        # against the current kubectl context
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-apchi}"
A=http://localhost:8000/api/v1

fail() { printf '\033[31mquickstart check failed: %s\033[0m\n' "$*" >&2; kubectl -n "$NAMESPACE" get pods -o wide >&2 || true; kubectl -n "$NAMESPACE" logs deploy/apchi --tail=100 >&2 || true; exit 1; }

"$ROOT/scripts/quickstart.sh"

kubectl -n "$NAMESPACE" port-forward svc/apchi 8000:8000 >/dev/null 2>&1 &
forward=$!
trap 'kill $forward 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf "$A/health" >/dev/null && break; sleep 1; done
curl -sf "$A/health" | grep -q '"status":"ok"' || fail "health is not ok"

curl -sf -X POST "$A/catalogs" -H 'content-type: application/json' \
  -d '{"name":"sales","connector":"tpch","properties":{}}' >/dev/null || fail "staging a catalog"
apply=$(curl -sf -X POST "$A/applies" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
for _ in $(seq 300); do
  stage=$(curl -sf "$A/applies/$apply" | python3 -c 'import sys,json; print(json.load(sys.stdin)["stage"])')
  case "$stage" in succeeded) break;; failed|incident) fail "the Apply ended $stage";; esac
  sleep 1
done
[[ "$stage" == succeeded ]] || fail "the Apply never finished; last stage $stage"

kubectl -n "$NAMESPACE" exec deploy/trino-coordinator -c trino -- \
  trino --execute "SELECT count(*) FROM sales.tiny.nation" 2>/dev/null | grep -q '"25"' \
  || fail "the applied catalog does not answer in Trino"

kill $forward 2>/dev/null || true
"$ROOT/scripts/quickstart.sh" --delete
! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || fail "--delete left the namespace behind"
echo "quickstart ok"
