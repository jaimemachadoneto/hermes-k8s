# Seam 2: runs the builder of the Nix base (`nix-base-build`) against temporary directories.
# The check needs no network and no cluster: a local Git repository stands in for GitHub, and a
# script stands in for `devenv`. It reads the image too, so that the builder in the image is the
# one that runs here.
{
  pkgs,
  image,
  builder,
  registration,
}:
pkgs.runCommand "nix-base-build-check"
  {
    nativeBuildInputs = [
      pkgs.git
      pkgs.nix
      pkgs.jq
      pkgs.gnutar
      pkgs.gzip
    ];
  }
  ''
    fail() { echo "FAIL: $*" >&2; failed=1; }
    failed=0
    export HOME=$TMPDIR/home
    mkdir -p "$HOME"
    build=${builder}/bin/nix-base-build

    # The builder is in the image, and the image has the list of its store paths.
    mkdir archive rootfs
    tar -xf ${image} -C archive 2>/dev/null || tar -xzf ${image} -C archive
    for layer in $(jq -r '.[0].Layers[]' archive/manifest.json); do
      tar -xf "archive/$layer" -C rootfs --exclude='dev/*' 2>/dev/null || true
      chmod -R u+w rootfs
    done
    [ -e rootfs/bin/nix-base-build ] || [ -L rootfs/bin/nix-base-build ] || fail "/bin/nix-base-build is not in the image"
    cmp -s ${registration} rootfs/etc/nix/registration || fail "/etc/nix/registration is not the registration of the image"
    [ -s ${registration} ] || fail "the registration is empty"

    # A store of the image: a read-only directory, as in the image.
    src=$TMPDIR/image-store
    mkdir -p "$src/pkg/bin"
    echo tool > "$src/pkg/bin/tool"
    chmod 555 "$src/pkg/bin"

    export NIX_BASE_SOURCE_STORE=$src
    export NIX_BASE_REGISTRATION=${registration}
    export NIX_BASE_REPOS=""

    # 1. The copy fills an empty volume, writes the marker and loads the database.
    vol=$TMPDIR/vol
    NIX_BASE_TARGET=$vol $build copy
    [ "$(cat "$vol/nix/store/pkg/bin/tool")" = tool ] || fail "the store is not copied"
    [ -e "$vol/nix/.closure-copied" ] || fail "no marker .closure-copied"
    [ -d "$vol/workspace" ] && [ -d "$vol/home" ] || fail "no workspace or home directory"
    [ -s "$vol/nix/var/nix/db/db.sqlite" ] || fail "the Nix database of the volume is empty"
    first=$(head -1 ${registration})
    NIX_STATE_DIR=$vol/nix/var/nix nix-store --check-validity "$first" 2>/dev/null \
      || fail "the database does not know $first"

    # 2. A volume with the marker is not copied again.
    mkdir -p "$vol/nix/store/kept"
    NIX_BASE_TARGET=$vol $build copy
    [ -d "$vol/nix/store/kept" ] || fail "a second copy ran on a volume with the marker"

    # 3. An incomplete copy (no marker) is replaced.
    rm "$vol/nix/.closure-copied"
    NIX_BASE_TARGET=$vol $build copy
    [ ! -e "$vol/nix/store/kept" ] || fail "an incomplete copy stayed"
    [ -e "$vol/nix/.closure-copied" ] || fail "no marker after the second copy"

    # 4. The warm step clones each repository in order and runs devenv shell in it.
    git config --global user.email t@example.com
    git config --global user.name t
    git config --global init.defaultBranch main
    for repo in owner/home-ops owner/hermes-k8s; do
      mkdir -p "git/$repo"
      git init -q "git/$repo"
      echo content > "git/$repo/devenv.nix"
      git -C "git/$repo" add devenv.nix
      git -C "git/$repo" commit -qm init
      git clone -q --bare "git/$repo" "git/$repo.git"
    done
    cat > devenv-stub <<'STUB'
    #!/bin/sh
    echo "$(basename "$PWD") $*" >> "$STUB_LOG"
    [ -e devenv.nix ] || exit 3
    # A NIX_CONFIG line with a token goes to the log as its key only.
    echo "config $(printf %s "$NIX_CONFIG" | sed 's/=.*//')" >> "$STUB_LOG"
    [ -z "$STUB_FAIL" ] || exit 1
    STUB
    chmod +x devenv-stub
    export STUB_LOG=$TMPDIR/devenv.log STUB_FAIL=
    export NIX_BASE_DEVENV=$TMPDIR/devenv-stub NIX_BASE_GIT_URL=file://$PWD/git
    export NIX_BASE_WORK=$TMPDIR/work NIX_BASE_TOKEN_FILE=$TMPDIR/no-token
    export NIX_BASE_REPOS="owner/home-ops owner/hermes-k8s"
    NIX_BASE_TARGET=$vol $build warm 2>/dev/null
    [ "$(grep -v '^config' "$STUB_LOG")" = "$(printf '%s\n' 'home-ops shell -- true' 'hermes-k8s shell -- true')" ] \
      || fail "devenv shell ran wrong: $(cat "$STUB_LOG")"
    [ ! -e "$NIX_BASE_WORK" ] || fail "the clones stayed"

    # 5. With a token file, Nix gets a token for GitHub. Git gets a helper, not the token.
    : > "$STUB_LOG"
    echo not-a-real-token > "$TMPDIR/token"
    NIX_BASE_TOKEN_FILE=$TMPDIR/token NIX_BASE_REPOS=owner/home-ops NIX_BASE_TARGET=$vol $build warm 2>/dev/null
    grep -q '^config access-tokens ' "$STUB_LOG" || fail "devenv got no token for GitHub: $(cat "$STUB_LOG")"
    ! grep -q 'not-a-real-token' "$STUB_LOG" || fail "the token is in the log"

    # 6. A failed devenv fails the builder, so the Job makes no snapshot.
    if STUB_FAIL=1 NIX_BASE_REPOS=owner/home-ops NIX_BASE_TARGET=$vol $build warm 2>/dev/null; then
      fail "the builder passed when devenv failed"
    fi

    # 7. A missing repository fails the builder.
    if NIX_BASE_REPOS=owner/missing NIX_BASE_TARGET=$vol $build warm 2>/dev/null; then
      fail "the builder passed when a clone failed"
    fi

    # 8. `all` copies and then warms.
    : > "$STUB_LOG"
    NIX_BASE_TARGET=$TMPDIR/vol2 NIX_BASE_REPOS=owner/hermes-k8s $build all 2>/dev/null
    [ -e "$TMPDIR/vol2/nix/.closure-copied" ] || fail "all: no copy"
    grep -q '^hermes-k8s shell' "$STUB_LOG" || fail "all: no devenv shell"

    [ "$failed" = 0 ] || exit 1
    touch $out
  ''
