# Check 2: run the builder of the Nix base (`nix-base-build`) against temporary directories.
# The check needs no network and no cluster. A local Git repository replaces GitHub. A script
# replaces `devenv`. The check reads the image too, so the builder that runs is the builder
# in the image.
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
      pkgs.python3
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
    [ -L rootfs/bin/nix-base-build ] || fail "/bin/nix-base-build is not in the image"
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

    # 4. The clone step clones each repository in order. The warm step runs devenv shell in each
    # clone and removes the clones. Neither gets a token when no token file exists.
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
    # The warm step must have no token in its environment.
    env | grep -q 'not-a-real-token\|GIT_CONFIG_VALUE' && echo "TOKEN-LEAK" >> "$STUB_LOG"
    [ -z "$STUB_FAIL" ] || exit 1
    STUB
    chmod +x devenv-stub
    export STUB_LOG=$TMPDIR/devenv.log STUB_FAIL=
    export NIX_BASE_DEVENV=$TMPDIR/devenv-stub NIX_BASE_GIT_URL=file://$PWD/git
    export NIX_BASE_WORK=$TMPDIR/work NIX_BASE_TOKEN_FILE=$TMPDIR/no-token
    export NIX_BASE_REPOS="owner/home-ops owner/hermes-k8s"
    NIX_BASE_TARGET=$vol $build clone 2>/dev/null
    [ -e "$NIX_BASE_WORK/home-ops/devenv.nix" ] && [ -e "$NIX_BASE_WORK/hermes-k8s/devenv.nix" ] \
      || fail "clone: a repository is missing"
    [ ! -e "$STUB_LOG" ] || fail "clone ran devenv"
    NIX_BASE_TARGET=$vol $build warm 2>/dev/null
    [ "$(cat "$STUB_LOG")" = "$(printf '%s\n' 'home-ops shell -- true' 'hermes-k8s shell -- true')" ] \
      || fail "devenv shell ran wrong: $(cat "$STUB_LOG")"
    [ ! -e "$NIX_BASE_WORK" ] || fail "the clones stayed"

    # 5. With a token file, git gets a helper that gives the token, and the token is not in an
    # argument. The warm step, which runs the code of the repositories, has no token.
    # A server that asks for credentials, and logs the Authorization header that it gets.
    cat > auth-server.py <<'SERVER'
    import base64, http.server, os
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization")
            if auth is None:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
            else:
                with open(os.environ["AUTH_LOG"], "a") as log:
                    log.write(base64.b64decode(auth.split()[1]).decode() + "\n")
                self.send_response(404)
            self.end_headers()
        def log_message(self, *args):
            pass
    http.server.HTTPServer(("127.0.0.1", 18765), Handler).serve_forever()
    SERVER
    export AUTH_LOG=$TMPDIR/auth.log
    ${pkgs.python3}/bin/python3 auth-server.py &
    server=$!
    for _ in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/18765) 2>/dev/null && break; sleep 0.1; done
    echo not-a-real-token > "$TMPDIR/token"
    NIX_BASE_GIT_URL=http://127.0.0.1:18765 NIX_BASE_TOKEN_FILE=$TMPDIR/token \
      NIX_BASE_REPOS=owner/private NIX_BASE_TARGET=$vol $build clone 2>/dev/null && fail "the private clone passed"
    kill $server
    grep -qx 'x-access-token:not-a-real-token' "$AUTH_LOG" || fail "the server got no token: $(cat "$AUTH_LOG" 2>/dev/null)"
    : > "$STUB_LOG"
    NIX_BASE_TOKEN_FILE=$TMPDIR/token NIX_BASE_REPOS=owner/home-ops NIX_BASE_TARGET=$vol $build clone 2>/dev/null
    NIX_BASE_TOKEN_FILE=$TMPDIR/token NIX_BASE_REPOS=owner/home-ops NIX_BASE_TARGET=$vol $build warm 2>/dev/null
    ! grep -q 'TOKEN-LEAK' "$STUB_LOG" || fail "the warm step has the token in its environment"

    # 6. A failed devenv fails the builder, so the Job makes no snapshot.
    NIX_BASE_REPOS=owner/home-ops NIX_BASE_TARGET=$vol $build clone 2>/dev/null
    if STUB_FAIL=1 NIX_BASE_TARGET=$vol NIX_BASE_REPOS=owner/home-ops $build warm >$TMPDIR/err 2>&1; then
      fail "the builder passed when devenv failed"
    fi
    grep -q 'devenv shell in owner/home-ops' "$TMPDIR/err" || fail "devenv did not run in step 6"

    # 7. A missing repository fails the clone step and runs no devenv.
    : > "$STUB_LOG"
    if NIX_BASE_REPOS=owner/missing NIX_BASE_TARGET=$vol $build clone 2>/dev/null; then
      fail "the builder passed when a clone failed"
    fi
    [ ! -s "$STUB_LOG" ] || fail "devenv ran after a failed clone"

    # 8. `all` does the three steps. An unknown step exits with code 2.
    : > "$STUB_LOG"
    NIX_BASE_TARGET=$TMPDIR/vol2 NIX_BASE_REPOS=owner/hermes-k8s $build all 2>/dev/null
    [ -e "$TMPDIR/vol2/nix/.closure-copied" ] || fail "all: no copy"
    grep -q '^hermes-k8s shell' "$STUB_LOG" || fail "all: no devenv shell"
    code=0
    $build bogus 2>/dev/null || code=$?
    [ "$code" = 2 ] || fail "an unknown step exits with $code, want 2"

    [ "$failed" = 0 ] || exit 1
    touch $out
  ''
