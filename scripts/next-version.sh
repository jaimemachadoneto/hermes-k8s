#!/usr/bin/env bash
# Print the next release tag. The rule is the rule of `deploy patch|minor|major`: the highest
# tag vX.Y.Z of the repository (by version, not by text) gets the bump. Other tags, such as
# v1.0.0-rc1, do not count. With no tag, the start is v0.0.0.
# Usage: next-version.sh patch|minor|major (in the repository, with its tags fetched)
set -euo pipefail

bump=${1:-}
case "$bump" in
  patch | minor | major) ;;
  *)
    echo "next-version: give patch, minor or major, not '$bump'" >&2
    exit 2
    ;;
esac

last=$(git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)
last=${last:-v0.0.0}
IFS=. read -r major minor patch <<<"${last#v}"
case "$bump" in
  major) major=$((major + 1)) minor=0 patch=0 ;;
  minor) minor=$((minor + 1)) patch=0 ;;
  patch) patch=$((patch + 1)) ;;
esac
echo "v$major.$minor.$patch"
