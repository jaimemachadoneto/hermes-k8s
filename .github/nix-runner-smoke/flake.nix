# A small image for the smoke test of the runner arc-jmn-nix. The test builds
# it with dockerTools, as the sandbox image and the Hermes image will be
# built, and the second run must find it in the persistent Nix store.
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { nixpkgs, ... }:
    let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
    in
    {
      packages.x86_64-linux.image = pkgs.dockerTools.buildLayeredImage {
        name = "hermes-k8s-runner-smoke";
        tag = "latest";
        contents = [ pkgs.hello ];
        config = {
          User = "10000:10000";
          Cmd = [ "/bin/hello" ];
        };
      };
    };
}
