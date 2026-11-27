# The `default` package of the Hermes flake, built from the pinned source with
# the patches in nix/patches/ (ADR 0012). It calls the package module of the
# Hermes flake on the patched source, so the package keeps the same dependency
# groups and build as upstream. The build fails when a patch does not apply.
#
# The module comes from the patched source, which is a derivation, so the
# evaluation imports from a derivation. The patches change only Python files.
{
  hermes,
  system,
}:
let
  pkgs = hermes.inputs.nixpkgs.legacyPackages.${system};
  inherit (pkgs) lib;

  patches = map (name: ./patches + "/${name}") (
    lib.filter (lib.hasSuffix ".patch") (lib.attrNames (builtins.readDir ./patches))
  );

  src = pkgs.applyPatches {
    name = "hermes-agent-source";
    src = hermes;
    inherit patches;
  };

  # The flake-parts module of upstream; `self` gives the revision metadata.
  module = import "${src}/nix/packages.nix" {
    inputs = hermes.inputs // {
      self = hermes;
    };
  };
in
(module.perSystem {
  inherit pkgs lib;
  inputs' = { };
}).packages.default
