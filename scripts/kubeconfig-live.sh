#!/usr/bin/env bash
# Write a temporary kubeconfig for the ServiceAccount hermes-sandbox-client of the namespace ai.
# The token lives for one hour, so the live suite runs with the rights of ticket 07 and no more.
# Your own kubeconfig (KUBECONFIG or ~/.kube/config) creates the token and gives the server and
# the CA. The new file is mode 0600. Delete it when the test ends. Print only its path.
# Usage: KUBECONFIG_OUT=/path scripts/kubeconfig-live.sh   (default: a new file in $TMPDIR)
set -euo pipefail

out="${KUBECONFIG_OUT:-$(mktemp "${TMPDIR:-/tmp}/hermes-live-kubeconfig.XXXXXX")}"
umask 077

server="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
ca="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
if [[ -z "${server}" || -z "${ca}" ]]; then
  echo "FAIL: your kubeconfig needs a server and certificate-authority-data" >&2
  exit 1
fi
token="$(kubectl create token -n ai hermes-sandbox-client --duration=1h)"

cat >"${out}" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: home-ops
    cluster:
      server: ${server}
      certificate-authority-data: ${ca}
users:
  - name: hermes-sandbox-client
    user:
      token: ${token}
contexts:
  - name: live
    context:
      cluster: home-ops
      user: hermes-sandbox-client
      namespace: hermes-sandbox
current-context: live
YAML
chmod 600 "${out}"
echo "${out}"
