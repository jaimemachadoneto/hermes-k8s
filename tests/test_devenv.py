"""The development shell has the tools that the verbs need."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_shell_has_kubectl_for_the_kubeconfig_verb():
    devenv = (ROOT / "devenv.nix").read_text()
    script = (ROOT / "scripts" / "kubeconfig-live.sh").read_text()

    assert "base.commands.kubeconfig-live" in devenv
    assert re.search(r"\bkubectl\b", script)
    # The packages list is the first list of the file: it ends at the first "];".
    packages = devenv.split("packages = with pkgs; [", 1)[1].split("];", 1)[0]
    assert re.search(r"^\s*kubectl\b", packages, re.MULTILINE)
