# A terminal-backend plugin gives each session its own sandbox

With `terminal.backend: local`, each shell runs in the Hermes pod and can
read every secret of Hermes. We write a Hermes terminal-backend plugin that
makes one sandbox for each session in the namespace `hermes-sandbox`. The
unit of isolation is the session: `delegate_task` subagents share the
sandbox of their parent session.

## Considered Options

- The built-in `ssh` backend: one shared host for all sessions, so no
  isolation between sessions.
- The built-in `docker` backend: it needs a Docker daemon, which the cluster
  does not give to a pod without privileges.
