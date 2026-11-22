# A sandbox reads its secrets from a Secret in its own namespace

`pods/exec` cannot set environment variables. If the plugin puts a token in
the command, the token goes into the exec URL, and the audit log of the API
server can record it. Thus an ExternalSecret writes the GitHub tokens from
the 1Password item `hermes` into the Secret `hermes-sandbox-<profile>` in
`hermes-sandbox`. The Sandbox mounts it read-only at `/run/secrets/github/`,
and a login script exports the tokens. The admission policy allows only
Secrets with that name pattern.

## Considered Options

- The plugin writes the tokens into the exec stdin of each command: the
  tokens then stay in the Hermes pod and go through it for each command.
- A volume that both the Hermes pod and the sandbox mount: a PVC belongs to
  one namespace, so this needs a second static CephFS PV, and each sandbox
  can read the files of each profile.

## Consequences

- The Hermes pod keeps `GH_TOKEN` and `GITHUB_TOKEN` for its own features:
  the Skills Hub, its own git clones, the Kanban PR check and the MCP
  servers that it starts. `GITHUB_TOKEN_PERSONAL` and `GIT_CONFIG_SYSTEM`
  go out of the Hermes pod.
- `terminal.env_passthrough` is empty. The backend lists the GitHub keys in
  `strip_env_keys`.
- A token in a command of the sandbox is readable by that command. This is
  necessary, because `git` and `gh` use it.
