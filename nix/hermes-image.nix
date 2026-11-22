# The Hermes image (ADR 0006): the default package of the Hermes flake, the
# `claude` CLI, and one read-only directory of bundled plugins. The pod runs
# it as uid 10000 with a volume at /opt/data. The image has no secret: the
# OAuth token comes from the cluster at run time (ADR 0005).
{
  pkgs,
  hermes,
  claude-code,
  subscription-plugin,
}:
let
  uid = "10000";

  # The Python client of the hermes-k8s plugin. It goes in through the extra
  # Python packages of the Hermes flake, from the Python of that package. The
  # flake refuses a package that the sealed venv of Hermes has already, so the
  # client keeps only the dependency that the venv lacks. The venv has the
  # other dependencies at their own versions.
  kubernetes = hermes.python.pkgs.kubernetes.overridePythonAttrs (old: {
    dependencies = [ hermes.python.pkgs.durationpy ];
    propagatedBuildInputs = [ ];
    dontCheckRuntimeDeps = true;
    doCheck = false;
    pythonImportsCheck = [ ];
  });
  hermes-with-kubernetes = hermes.override {
    extraPythonPackages = [ kubernetes ];
  };

  # The stock plugins, the subscription plugin and the hermes-k8s plugin.
  # Hermes registers a plugin under its manifest name, so the directory names
  # do not matter for a top-level plugin. The tests of the plugin stay out.
  bundled-plugins = pkgs.runCommand "hermes-bundled-plugins" { } ''
    cp -rL ${hermes-with-kubernetes}/share/hermes-agent/plugins $out
    chmod -R u+w $out
    mkdir $out/claude-subscription-directsdk
    cp -rL ${subscription-plugin}/claude-subscription-directsdk/. $out/claude-subscription-directsdk
    chmod -R u+w $out
    rm -rf $out/claude-subscription-directsdk/tests $out/claude-subscription-directsdk/evals
    mkdir $out/k8s_sandbox
    cp -r ${../plugin/k8s_sandbox}/. $out/k8s_sandbox
    # The fake cluster is for tests only.
    rm -f $out/k8s_sandbox/fake_cluster.py
    find $out -name __pycache__ -prune -exec rm -rf {} +
  '';

  # The wrapper of Hermes exports HERMES_BUNDLED_PLUGINS itself, so the
  # image environment cannot change it. Rewrite the wrapper scripts instead.
  hermes-wrapped = pkgs.runCommand "hermes-wrapped" { } ''
    mkdir -p $out/bin
    for f in ${hermes-with-kubernetes}/bin/*; do
      sed -e 's|${hermes-with-kubernetes}/share/hermes-agent/plugins|${bundled-plugins}|g' \
          -e 's|${hermes-with-kubernetes}/bin|'$out'/bin|g' "$f" > "$out/bin/$(basename "$f")"
      chmod +x "$out/bin/$(basename "$f")"
    done
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/jaimemachadoneto/hermes";
  contents = [
    hermes-wrapped
    claude-code
    pkgs.dockerTools.binSh
    pkgs.dockerTools.caCertificates
    pkgs.coreutils
    pkgs.tini
  ];
  extraCommands = ''
    mkdir -p opt/data tmp etc

    # Real files, so the user lookup works without the store.
    printf '%s\n' 'root:x:0:0::/root:/bin/sh' \
      'hermes:x:${uid}:${uid}::/opt/data:/bin/sh' > etc/passwd
    printf '%s\n' 'root:x:0:' 'hermes:x:${uid}:' > etc/group
    chmod 1777 tmp
  '';
  fakeRootCommands = ''
    chown ${uid}:${uid} opt/data
  '';
  config = {
    User = "${uid}:${uid}";
    WorkingDir = "/opt/data";
    Entrypoint = [
      "${pkgs.tini}/bin/tini"
      "--"
    ];
    Cmd = [
      "hermes"
      "gateway"
      "run"
    ];
    Env = [
      "HOME=/opt/data"
      "HERMES_HOME=/opt/data"
      "HERMES_WRITE_SAFE_ROOT=/opt/data"
      "HERMES_BUNDLED_PLUGINS=${bundled-plugins}"
      "PATH=/bin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
    ];
  };
}
