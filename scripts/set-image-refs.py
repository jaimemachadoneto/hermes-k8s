#!/usr/bin/env python3
"""Set the two image references of the Hermes HelmRelease in home-ops.

Only text lines change, so the diff of the PR shows two lines. A reference without
a digest is refused: the admission policy of the sandbox refuses it too.
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
    args.file.write_text(text)


if __name__ == "__main__":
    main()
