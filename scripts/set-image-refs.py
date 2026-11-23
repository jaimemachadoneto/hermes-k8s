#!/usr/bin/env python3
"""Set the image references of Hermes in home-ops.

The HelmRelease gets the Hermes tag and the sandbox image. With --nix-base-file, each sandbox
image of the Nix base Job gets the same sandbox reference. The plugin finds a Nix base only
when the two digests are equal. Only text lines change. The diff of the PR shows only these
lines. The script refuses a reference without a digest. The admission policy of the sandbox
refuses it too. The script changes no file when a check fails.
"""

import argparse
import re
import sys
from pathlib import Path

HERMES = "ghcr.io/jaimemachadoneto/hermes"
SANDBOX = "ghcr.io/jaimemachadoneto/hermes-sandbox"
DIGEST = r"sha256:[a-f0-9]{64}"
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"


def replace_once(text: str, pattern: str, replacement, what: str) -> str:
    new, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        sys.exit(f"set-image-refs: expected 1 match for {what}, found {count}")
    return new


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--hermes-tag", required=True, help="TAG@sha256:DIGEST")
    parser.add_argument("--sandbox-ref", required=True, help=f"{SANDBOX}:TAG@sha256:DIGEST")
    parser.add_argument("--nix-base-file", type=Path, help="nix-base.yaml of the sandbox tenant")
    args = parser.parse_args()

    if not re.fullmatch(f"{TAG}@{DIGEST}", args.hermes_tag):
        sys.exit(f"set-image-refs: bad Hermes tag: {args.hermes_tag}")
    if not re.fullmatch(re.escape(SANDBOX) + f"(:{TAG})?@{DIGEST}", args.sandbox_ref):
        sys.exit(f"set-image-refs: bad sandbox reference: {args.sandbox_ref}")

    text = args.file.read_text()
    text = replace_once(
        text,
        rf"^(\s*repository: {re.escape(HERMES)}\n\s*tag: ).*$",
        lambda m: m.group(1) + args.hermes_tag,
        "the Hermes image",
    )
    text = replace_once(
        text,
        r"^(\s*HERMES_K8S_SANDBOX_IMAGE: ).*$",
        lambda m: m.group(1) + args.sandbox_ref,
        "HERMES_K8S_SANDBOX_IMAGE",
    )

    nix_base = None
    if args.nix_base_file:
        # Any form of the sandbox image is replaced, so an old line with no digest stays out.
        # The `image:` lines of other images (the `snapshot` container) never match.
        nix_base, count = re.subn(
            rf"^([ \t-]*image: ){re.escape(SANDBOX)}(?=[:@\s]|$)\S*",
            lambda m: m.group(1) + args.sandbox_ref,
            args.nix_base_file.read_text(),
            flags=re.MULTILINE,
        )
        if count == 0:
            sys.exit(f"set-image-refs: no sandbox image line in {args.nix_base_file}")
        # A line in a form that the pattern skips (a quoted value) would keep the old digest.
        named = len(
            re.findall(
                rf"^.*\bimage:.*{re.escape(SANDBOX)}", args.nix_base_file.read_text(), re.MULTILINE
            )
        )
        if named != count:
            sys.exit(
                f"set-image-refs: {named - count} sandbox image line(s) in {args.nix_base_file} not replaced"
            )

    args.file.write_text(text)
    if nix_base is not None:
        args.nix_base_file.write_text(nix_base)


if __name__ == "__main__":
    main()
