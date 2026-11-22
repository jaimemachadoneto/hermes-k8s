#!/usr/bin/env python3
"""Check that tests/vap_rules.py has the image pattern of the policy in home-ops.

Usage: check-vap-drift.py PATH-TO-validatingadmissionpolicy.yaml

This checks the image pattern only. The other rules are CEL code that has no simple
Python equivalent. When one of them changes, compare it by hand.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import vap_rules


def policy_pattern(text: str) -> str:
    match = re.search(r"name: imagePattern\s+expression: >-\s+'(\^.*\$)'", text)
    if not match:
        sys.exit("check-vap-drift: the policy has no variable imagePattern")
    # CEL reads \\ as one backslash.
    return match.group(1).replace("\\\\", "\\")


def main() -> None:
    expected = policy_pattern(Path(sys.argv[1]).read_text())
    actual = "^" + vap_rules.IMAGE_PATTERN.pattern + "$"
    if expected != actual:
        sys.exit(
            "check-vap-drift: imagePattern differs.\n"
            f"  home-ops:  {expected}\n  vap_rules: {actual}"
        )
    print("imagePattern is the same in home-ops and in tests/vap_rules.py")


if __name__ == "__main__":
    main()
