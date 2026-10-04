#!/usr/bin/env bash
# Brings up a Trino and an Apchi on a local cluster, from nothing, in one command.
#
#   ./scripts/quickstart.sh
#
# Everything lands in one namespace (default: apchi) and `--delete` removes all of it.
# This is the development path: one replica of everything, no TLS, no authentication,
# and Snapshots on an emptyDir. docs/getting-started.md says what to do next.
set -euo pipefail

NAMESPACE="${NAMESPACE:-apchi}"
RELEASE="${RELEASE:-apchi}"
IMAGE="${IMAGE:-apchi:dev}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

for tool in kubectl helm docker; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach a cluster; start minikube or kind first"

if [[ "${1:-}" == "--delete" ]]; then
  say "Removing everything"
  helm uninstall "$RELEASE" -n "$NAMESPACE" 2>/dev/null || true
  kubectl delete -f "$ROOT/deploy/trino-dev/" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
  kubectl delete namespace "$NAMESPACE" --ignore-not-found
  echo "Gone."
  exit 0
fi

# Which local cluster this is decides how the image gets in: neither minikube nor kind
# can pull from the host's Docker daemon, and the error if you skip this is an
# ImagePullBackOff that looks like a registry problem.
context="$(kubectl config current-context)"
say "Building $IMAGE into the cluster ($context)"
case "$context" in
  minikube)
    minikube image build -t "$IMAGE" "$ROOT" ;;
  kind-*)
    docker build -t "$IMAGE" "$ROOT"
    kind load docker-image "$IMAGE" --name "${context#kind-}" ;;
  *)
    docker build -t "$IMAGE" "$ROOT"
    echo "Context '$context' is not minikube or kind. The image was built locally;"
    echo "push it somewhere the cluster can pull from and re-run with"
    echo "  IMAGE=<your registry>/apchi:dev $0"
    [[ "${ALLOW_ANY_CONTEXT:-}" == "1" ]] || die "refusing to guess how to deliver the image" ;;
esac

say "Namespace $NAMESPACE"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# Apchi configures a Trino; it does not install one. This is the reference Trino that
# satisfies Apchi's preconditions -- see deploy/trino-dev/README.md.
say "Trino"
kubectl apply -f "$ROOT/deploy/trino-dev/" -n "$NAMESPACE"
kubectl rollout status deploy/trino-coordinator -n "$NAMESPACE" --timeout=600s
kubectl rollout status deploy/trino-worker -n "$NAMESPACE" --timeout=600s

say "Apchi"
helm upgrade --install "$RELEASE" "$ROOT/charts/apchi" \
  --namespace "$NAMESPACE" \
  --set image.repository="${IMAGE%:*}" \
  --set image.tag="${IMAGE##*:}" \
  --set mongodb.deploy=true \
  --wait --timeout 10m

cat <<EOF

$(printf '\033[1mReady.\033[0m') Open the API:

  kubectl -n $NAMESPACE port-forward svc/$RELEASE 8000:8000

and in another terminal:

  curl -s localhost:8000/api/v1/health
  curl -s -X POST localhost:8000/api/v1/catalogs -H 'content-type: application/json' \\
    -d '{"name":"sales","connector":"tpch","properties":{}}'
  curl -s localhost:8000/api/v1/review

localhost:8000/docs is the whole API. docs/getting-started.md walks one catalog through
review, validation, apply and rollback.

Remove everything:  $0 --delete
EOF
