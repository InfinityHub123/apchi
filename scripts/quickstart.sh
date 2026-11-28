#!/usr/bin/env bash
# Brings up a Trino and an Apchi on a local cluster, from nothing, in one command.
#
#   ./scripts/quickstart.sh
#
# Everything lands in one namespace (default: apchi) and `--delete` removes all of it.
# This is the development path: one replica of everything, no TLS, no authentication,
# and Snapshots on an emptyDir. docs/getting-started.md says what to do next.
#
# Against the current kubectl context: a kind cluster of any name, or a minikube profile of
# any name. Run it again after changing Apchi and it rebuilds the image and restarts Apchi on
# it, leaving Trino and the Snapshots as they are. NAMESPACE, RELEASE and IMAGE override the
# defaults.
set -euo pipefail

NAMESPACE="${NAMESPACE:-apchi}"
RELEASE="${RELEASE:-apchi}"
IMAGE="${IMAGE:-apchi:dev}"
# What the chart names Apchi's Deployment and Service: the release, or the release with
# "-apchi" after it when the release name does not already say apchi.
[[ "$RELEASE" == *apchi* ]] && APCHI="$RELEASE" || APCHI="$RELEASE-apchi"
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
  # Deleting the namespace is what removes the Secrets Apchi writes: no release owns them,
  # so helm uninstall leaves them -- they hold the Cluster's configuration and the only
  # durable copy of its catalogs.
  kubectl delete namespace "$NAMESPACE" --ignore-not-found
  echo "Gone."
  exit 0
fi

# Which local cluster this is decides how the image gets in: neither minikube nor kind
# can pull from the host's Docker daemon, and the error if you skip this is an
# ImagePullBackOff that looks like a registry problem.
context="$(kubectl config current-context)"
say "Building $IMAGE into the cluster ($context)"
# A minikube context is named after its profile, so `minikube` is only the default one.
minikube_profile() {
  command -v minikube >/dev/null &&
    minikube profile list -o json 2>/dev/null | grep -q "\"Name\":\"$context\""
}
if [[ "$context" == minikube ]] && ! command -v minikube >/dev/null; then
  die "the context is minikube but minikube is not installed"
fi
cluster="$context"
if [[ "$context" == minikube ]] || minikube_profile; then
  cluster=minikube
fi
case "$cluster" in
  minikube)
    minikube -p "$context" image build -t "$IMAGE" "$ROOT" ;;
  kind-*)
    command -v kind >/dev/null || die "the context is $context but kind is not installed"
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

# One release: Apchi, its Trino and a MongoDB. Plain HTTP and the name a client sends, so
# there are no certificates to make first; a real install keeps the chart's default, HTTPS,
# and provides its server certificate. The workers drain for seconds rather than the
# production two minutes, twice over, which is what --delete and every worker restart wait.
say "Apchi and Trino"
installed=$(helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1 && echo yes || true)
helm upgrade --install "$RELEASE" "$ROOT/charts/apchi" \
  --namespace "$NAMESPACE" \
  --set image.repository="${IMAGE%:*}" \
  --set image.tag="${IMAGE##*:}" \
  --set mongodb.deploy=true \
  --set trino.worker.replicas=1 \
  --set trino.https.enabled=false \
  --set trino.worker.gracefulShutdown.gracePeriodSeconds=5 \
  --set trino.worker.terminationGracePeriodSeconds=30 \
  --wait --timeout 10m

# A re-run rebuilt the image under the same tag, which Kubernetes cannot tell from the one
# already running: the pod template is unchanged, so nothing would restart and the old code
# would keep serving. Only Apchi's pod -- Trino and the MongoDB holding the Snapshots stay up.
if [[ -n "$installed" ]]; then
  say "Restarting Apchi on the image just built"
  kubectl -n "$NAMESPACE" rollout restart deploy/"$APCHI"
  kubectl -n "$NAMESPACE" rollout status deploy/"$APCHI" --timeout=5m
fi

cat <<EOF

$(printf '\033[1mReady.\033[0m') Open the API:

  kubectl -n $NAMESPACE port-forward svc/$APCHI 8000:8000

and in another terminal:

  curl -s localhost:8000/api/v1/health
  curl -s -X POST localhost:8000/api/v1/catalogs -H 'content-type: application/json' \\
    -d '{"name":"sales","connector":"tpch","properties":{}}'
  curl -s localhost:8000/api/v1/review

localhost:8000/docs is the whole API. docs/getting-started.md walks one catalog through
review, validation, apply and rollback.

Remove everything:  $0 --delete
EOF
