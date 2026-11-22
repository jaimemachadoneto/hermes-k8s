#!/usr/bin/env bash
# This script builds one image of the flake and pushes it to a registry. It
# prints the reference `IMAGE:FIRST_TAG@sha256:...` on stdout. The cluster pins an image by digest,
# so the caller reads the digest from this line.
#
# Usage: publish-image.sh ATTR IMAGE TAG [TAG...]
# Environment: REGISTRY_USER and REGISTRY_TOKEN (the token never goes in an argument).
set -euo pipefail

attr=${1:?usage: publish-image.sh ATTR IMAGE TAG [TAG...]}
image=${2:?usage: publish-image.sh ATTR IMAGE TAG [TAG...]}
shift 2
[ $# -ge 1 ] || { echo "publish-image: give at least one tag" >&2; exit 2; }
: "${REGISTRY_USER:?}" "${REGISTRY_TOKEN:?}"

cd "$(dirname "$0")/.."
out=$(nix build ".#$attr" --no-link --print-out-paths)

# The login file is temporary: the token must not stay on disk. `set -e` stops
# the script when a skopeo call fails, so a failed push is never hidden.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
auth=$work/auth.json
echo "{}" >"$auth"
printf '%s' "$REGISTRY_TOKEN" |
  skopeo login --authfile "$auth" "${image%%/*}" -u "$REGISTRY_USER" --password-stdin >&2

first=$1
shift
skopeo --insecure-policy copy --quiet --authfile "$auth" --digestfile "$work/digest" \
  "docker-archive:$out" "docker://$image:$first" >&2
digest=$(<"$work/digest")
[[ $digest =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "publish-image: bad digest '$digest'" >&2; exit 1; }

# The other tags point to the same digest, so every tag names one image.
for tag in "$@"; do
  skopeo --insecure-policy copy --quiet --preserve-digests --authfile "$auth" \
    "docker://$image@$digest" "docker://$image:$tag" >&2
done

echo "$image:$first@$digest"
