#!/usr/bin/env python3
"""Delete the old commit-tagged versions of one container package on ghcr.io.

CI pushes each image of `main` as `sha-<commit>`. The release adds the tag `X.Y.Z` to the
same version. This script deletes a version only when all of these are true:

- the version has at least one tag, and each tag has the form `sha-<40 hex digits>`;
- the version is older than the age limit (DEFAULT_DAYS, 30 days).

A version with a release tag, any other tag or no tag stays. An untagged version can be a
platform image or an attestation of another version, so it stays.

Usage: prune-versions.py --owner OWNER --package NAME [--days N] [--dry-run]
Environment: GITHUB_TOKEN (the token never goes in an argument).
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request

COMMIT_TAG = re.compile(r"sha-[0-9a-f]{40}")
API = "https://api.github.com"
DEFAULT_DAYS = 30


def select_deletable(versions, now, days=DEFAULT_DAYS):
    """Return the versions that the retention rule allows us to delete."""
    if days < 1:
        raise ValueError("the age limit must be at least one day")
    limit = dt.timedelta(days=days)
    chosen = []
    for item in versions:
        # Unknown data is never a reason to delete.
        container = (item.get("metadata") or {}).get("container") or {}
        tags = container.get("tags")
        if not isinstance(tags, list) or not tags:
            continue
        if not all(isinstance(t, str) and COMMIT_TAG.fullmatch(t) for t in tags):
            continue
        try:
            created = dt.datetime.fromisoformat(str(item.get("created_at")))
        except ValueError:
            continue
        if created.tzinfo is None:
            continue
        if now - created > limit:
            chosen.append(item)
    return chosen


class GithubApi:
    """The versions of one container package of an organization."""

    def __init__(self, owner, package, token):
        self.base = f"{API}/orgs/{owner}/packages/container/{package}/versions"
        self.token = token

    def _request(self, url, method="GET"):
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        return urllib.request.urlopen(request, timeout=30)

    def list_versions(self):
        url = f"{self.base}?per_page=100"
        found = []
        while url:
            with self._request(url) as response:
                found.extend(json.loads(response.read()))
                link = response.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        return found

    def delete_version(self, version_id):
        with self._request(f"{self.base}/{int(version_id)}", method="DELETE"):
            pass


def run(api, now, days=DEFAULT_DAYS, dry_run=False):
    """Delete the selected versions. Return how many versions were selected."""
    chosen = select_deletable(api.list_versions(), now, days)
    for item in chosen:
        tags = ",".join(item["metadata"]["container"]["tags"])
        print(f"{'would delete' if dry_run else 'delete'} version {item['id']} ({tags})")
        if not dry_run:
            api.delete_version(item["id"])
    return len(chosen)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("prune-versions: set GITHUB_TOKEN", file=sys.stderr)
        return 2
    api = GithubApi(args.owner, args.package, token)
    count = run(api, dt.datetime.now(dt.UTC), args.days, args.dry_run)
    print(f"{args.package}: {count} version(s) selected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
