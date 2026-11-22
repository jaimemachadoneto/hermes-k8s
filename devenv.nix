{ pkgs, ... }:
{
  name = "hermes-k8s";

  # Stacks come from devenv.stacks.nix (written by `base link`), pulled in
  # by devenv.yaml. Put project-specific tools here; machine-only settings go
  # in devenv.local.nix.
  packages = with pkgs; [
    skopeo # for push-image
  ];

  # `check` stays the verb of the Python stack. This verb reads the image.
  base.commands.flake-check = [ "nix flake check" ];

  base.commands.build = [
    "nix build .#sandbox-image --no-link"
    "nix build .#hermes-image --no-link"
  ];

  # Manual push of the sandbox image. The token of `gh` needs the scope
  # `write:packages`. It goes to skopeo through stdin, never to a file. The
  # cluster pins the image by the digest that this verb prints.
  base.commands.push-image = [
    ''
      out=$(nix build .#sandbox-image --no-link --print-out-paths)
      ref="ghcr.io/jaimemachadoneto/hermes-sandbox:$(basename "$out" | cut -c1-12)"
      gh auth token | skopeo login ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null
      skopeo --insecure-policy copy --quiet --digestfile /dev/stdout "docker-archive:$out" "docker://$ref" | sed "s|^|$ref@|"
    ''
  ];

  # Manual push of the Hermes image, the same way. The package on ghcr.io
  # stays private.
  base.commands.push-hermes-image = [
    ''
      set -eo pipefail
      out=$(nix build .#hermes-image --no-link --print-out-paths)
      ref="ghcr.io/jaimemachadoneto/hermes:$(basename "$out" | cut -c1-12)"
      # The auth file is temporary: the token never stays on disk.
      auth=$(mktemp)
      trap 'rm -f "$auth"' EXIT
      gh auth token | skopeo login --authfile "$auth" ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null
      digest=$(skopeo --insecure-policy copy --quiet --authfile "$auth" --digestfile /dev/stdout "docker-archive:$out" "docker://$ref")
      [ -n "$digest" ] || { echo "push failed" >&2; exit 1; }
      echo "$ref@$digest"
    ''
  ];

  # env.DATABASE_URL = "...";
  # services.postgres.enable = true;
}
