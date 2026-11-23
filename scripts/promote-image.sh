#!/usr/bin/env bash
# This script adds tags to an image that is on a registry already. CI pushed the image as
# `sha-<commit>`. The release adds the tag `X.Y.Z` to the same image. It builds nothing.
# It prints the reference `IMAGE:SOURCE_TAG@sha256:...` on stdout. The cluster pins an image
# by digest, so the caller reads the digest from this line.
#
# Usage: promote-image.sh IMAGE SOURCE_TAG TAG [TAG...]
# Environment: REGISTRY_USER and REGISTRY_TOKEN (the token never goes in an argument).
set -euo pipefail

usage="usage: promote-image.sh IMAGE SOURCE_TAG TAG [TAG...]"
image=${1:?$usage}
source_tag=${2:?$usage}
shift 2
[ $# -ge 1 ] || { echo "promote-image: give at least one tag" >&2; exit 2; }
: "${REGISTRY_USER:?}" "${REGISTRY_TOKEN:?}"

# The login file is temporary: the token must not stay on disk.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
auth=$work/auth.json
echo "{}" >"$auth"
printf '%s' "$REGISTRY_TOKEN" |
  skopeo login --authfile "$auth" "${image%%/*}" -u "$REGISTRY_USER" --password-stdin >&2

digest_of() {
  # The digest of the raw manifest is exact for one image and for an image list.
  local sum
  sum=$(skopeo inspect --authfile "$auth" --raw --no-tags "docker://$image:$1" | sha256sum) || return 1
  echo "sha256:${sum%% *}"
}

digest=$(digest_of "$source_tag") || exit 1
[[ $digest =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "promote-image: bad digest '$digest'" >&2; exit 1; }

# `--all` copies each platform of an image list. `--preserve-digests` keeps the digest, so
# every tag names the image that CI built and scanned.
for tag in "$@"; do
  skopeo --insecure-policy copy --quiet --all --preserve-digests --authfile "$auth" \
    "docker://$image:$source_tag" "docker://$image:$tag" >&2
  copied=$(digest_of "$tag") || exit 1
  [ "$copied" = "$digest" ] || {
    echo "promote-image: tag '$tag' has digest '$copied', expected '$digest'" >&2
    exit 1
  }
done

echo "$image:$source_tag@$digest"
