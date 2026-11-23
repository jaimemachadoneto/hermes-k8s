#!/usr/bin/env bash
# Wait for the run of ci.yaml on a push to main for one commit. The release copies the images
# that this run pushed, so the release must not start before the run is green.
# Usage: wait-for-ci.sh <commit sha>
# Variables: GITHUB_REPOSITORY (owner/name), CI_WAIT_SECONDS (default 2700),
# CI_POLL_SECONDS (default 30). gh reads its token from GH_TOKEN.
set -euo pipefail

sha=$1
deadline=$((SECONDS + ${CI_WAIT_SECONDS:-2700}))
poll=${CI_POLL_SECONDS:-30}

while :; do
  # One line for each run of the commit: "<status> <conclusion>".
  runs=$(gh run list --repo "$GITHUB_REPOSITORY" --workflow ci.yaml --commit "$sha" \
    --branch main --event push --json status,conclusion \
    --jq '.[] | "\(.status) \(.conclusion)"')
  if grep -qx 'completed success' <<<"$runs"; then
    echo "wait-for-ci: CI on main passed for $sha"
    exit 0
  fi
  # A run that is not complete can still pass. Wait for it, also when an older run failed.
  if [ -n "$runs" ] && ! grep -qv '^completed ' <<<"$runs"; then
    echo "::error::CI on main did not pass for $sha: $(head -1 <<<"$runs"). Correct main, then tag a new release." >&2
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "::error::CI on main did not end in time for $sha. Look at the run, then run the release again." >&2
    exit 1
  fi
  echo "wait-for-ci: CI on main for $sha is not complete. Wait ${poll}s."
  sleep "$poll"
done
