# Reads the configuration and the files of the built Hermes image. The check
# fails when a plugin is missing from the bundled directory, when `claude` is
# older than the minimum, when the user is not 10000, or when a file of Hermes
# in the image does not have its patch (ADR 0012). A wrong image never reaches
# the registry.
{
  pkgs,
  image,
  claude-code,
  minClaudeVersion,
  plugins,
  patchMarkers,
}:
pkgs.runCommand "hermes-image-check"
  {
    nativeBuildInputs = [
      pkgs.jq
      pkgs.gnutar
      pkgs.gzip
    ];
  }
  ''
    fail() { echo "FAIL: $*" >&2; failed=1; }
    failed=0

    mkdir archive rootfs
    tar -xf ${image} -C archive 2>/dev/null || tar -xzf ${image} -C archive
    config=$(jq -r '.[0].Config' archive/manifest.json)
    for layer in $(jq -r '.[0].Layers[]' archive/manifest.json); do
      tar -xf "archive/$layer" -C rootfs --exclude='dev/*' 2>/dev/null || true
      chmod -R u+w rootfs
    done

    cfg() { jq -r "$1" "archive/$config"; }

    [ "$(cfg '.config.User')" = "10000:10000" ] || fail "User is $(cfg '.config.User'), want 10000:10000"
    [ "$(cfg '(.config.Cmd // []) | join(" ")')" = "hermes gateway run" ] || fail "Cmd is $(cfg '.config.Cmd | join(" ")')"
    env_of() { cfg '(.config.Env // [])[]' | sed -n "s/^$1=//p"; }
    [ "$(env_of HOME)" = "/opt/data" ] || fail "HOME is $(env_of HOME), want /opt/data"

    # ADR 0005: the image holds no token.
    if cfg '(.config.Env // [])[]' | grep -qiE 'token|sk-ant-|api_key'; then
      fail "the image environment holds a secret"
    fi
    if grep -rIlE 'sk-ant-(oat|api)[0-9]*-[A-Za-z0-9_-]{20,}' rootfs >/dev/null 2>&1; then
      fail "a file of the image holds a token"
    fi

    # A link in the image points into /nix/store, so resolve it in rootfs.
    resolve() {
      p=$1
      for _ in 1 2 3 4 5 6 7 8; do
        [ -L "rootfs$p" ] || break
        t=$(readlink "rootfs$p")
        case "$t" in /*) p=$t ;; *) p=$(dirname "$p")/$t ;; esac
      done
      [ -e "rootfs$p" ] && echo "$p"
    }

    # The plugins: each entry is "directory:manifest name" in the bundled directory.
    bundled=$(env_of HERMES_BUNDLED_PLUGINS)
    [ -n "$bundled" ] || fail "HERMES_BUNDLED_PLUGINS is not set in the image"
    dir=$(resolve "$bundled") || { fail "bundled directory $bundled is missing"; dir=; }
    for entry in ${pkgs.lib.escapeShellArgs plugins}; do
      d=''${entry%%:*}
      n=''${entry#*:}
      m="rootfs$dir/$d/plugin.yaml"
      if [ -z "$dir" ] || [ ! -f "$m" ]; then
        fail "plugin $d is missing from the bundled directory"
      elif ! grep -q "^name: $n\$" "$m"; then
        fail "plugin $d has not the manifest name $n"
      fi
    done

    # The wrapper of hermes sets the variable itself, so it must name the same directory.
    wrapper=$(resolve /bin/hermes) || { fail "/bin/hermes is missing"; wrapper=; }
    if [ -n "$wrapper" ] && ! grep -q "^export HERMES_BUNDLED_PLUGINS='$bundled'\$" "rootfs$wrapper"; then
      fail "the hermes wrapper does not use $bundled"
    fi

    # claude: the image must hold this package, and the package must be new enough.
    claude=$(resolve /bin/claude) || { fail "/bin/claude is missing"; claude=; }
    [ -z "$claude" ] || [ "$claude" = "${claude-code}/bin/claude" ] || fail "/bin/claude is $claude, not ${claude-code}/bin/claude"
    version=$(HOME=$TMPDIR ${claude-code}/bin/claude --version | awk '{print $1}')
    lowest=$(printf '%s\n' ${minClaudeVersion} "$version" | sort -V | head -n1)
    [ "$lowest" = "${minClaudeVersion}" ] || fail "claude is $version, want at least ${minClaudeVersion}"

    # ADR 0012: each copy of a patched file in the image must have the patch. At least one copy
    # must exist.
    for entry in ${pkgs.lib.escapeShellArgs patchMarkers}; do
      file=''${entry%%:*}
      marker=''${entry#*:}
      found=0
      for f in rootfs/nix/store/*/lib/python3*/site-packages/$file; do
        [ -f "$f" ] || continue
        found=1
        grep -qF -- "$marker" "$f" || fail "''${f#rootfs} does not have its patch"
      done
      [ "$found" = 1 ] || fail "the image has no $file"
    done

    [ "$failed" = 0 ] || exit 1
    touch $out
  ''
