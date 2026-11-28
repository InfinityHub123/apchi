#!/usr/bin/env bash
# The system test_operator_sections.py runs against, on the current kubectl context: Apchi and
# its Trino from charts/apchi in the chart's default shape -- HTTPS, sign-in by certificate --
# with end users holding certificates from one CA, and a PostgreSQL that accepts nothing but a
# client certificate. Every file the tests need lands in $WORK.
#
#   IMAGE=apchi:operator-sections WORK=/tmp/operator-sections tests/operator_sections/setup.sh
#
# The image must already be in the cluster (kind load docker-image).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="${WORK:?set WORK to a directory for the certificates}"
IMAGE="${IMAGE:?set IMAGE to the Apchi image loaded into the cluster}"
NAMESPACE=trino
mkdir -p "$WORK" && cd "$WORK"

# A CA and leaves with the extensions a strict verifier asks for: Python 3.13 refuses a CA
# without keyUsage and a leaf without an Authority Key Identifier.
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 2 -subj "/CN=operator-sections-ca" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
leaf() {
  local name=$1 subject=$2 usage=$3 san=${4:-}
  openssl req -newkey rsa:2048 -nodes -keyout "$name.key" -out "$name.csr" -subj "$subject" 2>/dev/null
  {
    echo "authorityKeyIdentifier=keyid,issuer"
    echo "basicConstraints=CA:FALSE"
    echo "keyUsage=digitalSignature,keyEncipherment"
    echo "extendedKeyUsage=$usage"
    [[ -n "$san" ]] && echo "subjectAltName=$san"
  } > "$name.ext"
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$name.crt" \
    -days 2 -extfile "$name.ext" 2>/dev/null
}
leaf trino "/CN=trino" serverAuth \
  "DNS:trino,DNS:trino.$NAMESPACE.svc,DNS:trino.$NAMESPACE.svc.cluster.local,DNS:localhost,IP:127.0.0.1"
leaf postgres "/CN=postgres" serverAuth "DNS:postgres,DNS:postgres.$NAMESPACE.svc"
leaf alice "/O=Acme/OU=finance/CN=alice" clientAuth
leaf bob "/O=Acme/OU=analytics/CN=bob" clientAuth
leaf carol "/O=Acme/OU=support/CN=carol" clientAuth
leaf dave "/O=Other/CN=dave" clientAuth
# The identity Trino's PostgreSQL catalog logs in as: PostgreSQL maps the certificate's CN.
leaf trino_reader "/CN=trino_reader" clientAuth
# Signed by nobody Trino trusts, claiming to be in finance.
openssl req -x509 -newkey rsa:2048 -nodes -keyout mallory.key -out mallory.crt -days 2 \
  -subj "/O=Acme/OU=finance/CN=mallory" 2>/dev/null

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
k() { kubectl -n "$NAMESPACE" "$@"; }
k create secret generic postgres-tls --from-file=server.crt=postgres.crt \
  --from-file=server.key=postgres.key --from-file=ca.crt >/dev/null
k create secret generic trino-tls --from-file=tls.crt=trino.crt --from-file=tls.key=trino.key \
  --from-file=ca.crt >/dev/null
k create secret generic operator-sections-ca --from-file=ca-bundle.crt=ca.crt >/dev/null
k apply -f "$HERE/postgres.yaml" >/dev/null
k rollout status deploy/postgres --timeout=300s

# The chart's default shape. caBundle puts the CA at one path on every Trino pod, which is how
# the PostgreSQL catalog verifies its server.
helm install apchi "$ROOT/charts/apchi" -n "$NAMESPACE" \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" \
  --set mongodb.deploy=true \
  --set trino.https.existingSecret=trino-tls \
  --set caBundle.existingSecret=operator-sections-ca \
  --set trino.worker.replicas=1 \
  --set trino.worker.gracefulShutdown.gracePeriodSeconds=5 \
  --set trino.worker.terminationGracePeriodSeconds=30 \
  --wait --timeout 10m >/dev/null
echo "operator-sections system ready in namespace $NAMESPACE"
