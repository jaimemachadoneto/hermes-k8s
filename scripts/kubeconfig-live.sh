#!/usr/bin/env bash
# Write a temporary kubeconfig for the ServiceAccount hermes-sandbox-client of the namespace ai.
# The token is valid for one hour. It has only the rights of ticket 07.
# The script uses your own kubeconfig (KUBECONFIG or ~/.kube/config) to create the token and to
# get the server and the certificate authority. The new file is mode 0600. Delete it when the test ends. Print only its path.
# Usage: KUBECONFIG_OUT=/path scripts/kubeconfig-live.sh   (default: a new file in $TMPDIR)
set -euo pipefail

umask 077
out="${KUBECONFIG_OUT:-$(mktemp "${TMPDIR:-/tmp}/hermes-live-kubeconfig.XXXXXX")}"
rm -f "${out}"

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
