# The sandbox image: the shared tools, Nix and devenv. The pod runs it as uid
# 10000 with a read-only root file system, and it mounts a volume at /nix and
# at /workspace. The volume at /nix hides the store of the image, so nothing
# here may depend on a Nix daemon or on state in /nix/var.
{ pkgs, pkgs-unstable }:
let
  uid = "10000";

  tools = with pkgs; [
    git
    gh
    openssh
    bashInteractive
    coreutils
    findutils
    gnugrep
    gnused
    gawk
    ripgrep
    fd
    jq
    curl
    gnutar
    gzip
    unzip
    which
    python3
    nix
    direnv
    pkgs-unstable.devenv
  ];

  # The builder of a Nix base (ADR 0003). The Nix base Job runs it, and the init step of a
  # sandbox does the same copy in a shorter form.
  builder = pkgs.writeShellApplication {
    name = "nix-base-build";
    runtimeInputs = with pkgs; [
      coreutils
      git
      nix
      pkgs-unstable.devenv
    ];
    text = builtins.readFile ./nix-base-build.sh;
  };

  contents = tools ++ [
    builder
    pkgs.dockerTools.binSh
    pkgs.dockerTools.caCertificates
    pkgs.tini
  ];

  # The Nix database of the volume lists no path at the start. This file lists each store path
  # of the image, so that Nix keeps the paths that the copy brings instead of fetching them.
  registration = "${pkgs.closureInfo { rootPaths = contents; }}/registration";
in
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/jaimemachadoneto/hermes-sandbox";
  inherit contents;
  extraCommands = ''
    mkdir -p usr/bin workspace tmp home/sandbox etc/nix

    # These are real files, not links into the store: the volume at /nix hides
    # the store, and the user lookup must work before the Nix base is ready.
    # Kubernetes resolves the user by number, but some CLIs read the name and
    # the home directory from /etc/passwd.
    printf '%s\n' 'root:x:0:0::/root:/bin/sh' \
      'sandbox:x:${uid}:${uid}::/home/sandbox:/bin/bash' > etc/passwd
    printf '%s\n' 'root:x:0:' 'sandbox:x:${uid}:' > etc/group

    # No daemon and no build users: Nix runs as one user. The Nix sandbox
    # needs user namespaces, which the pod does not allow (ADR 0003).
    printf '%s\n' 'sandbox = false' 'build-users-group =' \
      'experimental-features = nix-command flakes' > etc/nix/nix.conf

    # The init step of a sandbox and the builder read this file.
    cp ${registration} etc/nix/registration

    ln -s ${pkgs.tini}/bin/tini usr/bin/tini
    ln -s ${pkgs.coreutils}/bin/env usr/bin/env
    chmod 1777 tmp
  '';
  fakeRootCommands = ''
    chown ${uid}:${uid} workspace home/sandbox
  '';
  config = {
    User = "${uid}:${uid}";
    WorkingDir = "/workspace";
    Entrypoint = [
      "/usr/bin/tini"
      "--"
    ];
    Cmd = [
      "sleep"
      "infinity"
    ];
    Env = [
      "HOME=/home/sandbox"
      "USER=sandbox"
      "PATH=/bin:/usr/bin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
    ];
  };
}
// {
  inherit builder registration;
}
