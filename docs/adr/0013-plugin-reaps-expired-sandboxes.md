---
status: accepted
---

# The plugin archives and deletes an expired sandbox

This ADR replaces the part of ADR 0002 that gives the end of the life of a
sandbox to `shutdownTime`.

A sandbox expires when it is suspended and its last use is more than 7 days
old. The plugin reads the last use from the annotation `hermes-k8s/last-used`,
which each command writes. The reaper of the plugin runs when Hermes starts
and then each hour. For each expired sandbox, it makes a sandbox archive (a
VolumeSnapshot with the labels `hermes-k8s/kind: sandbox-archive`,
`hermes-k8s/sandbox` and `hermes-k8s/expires`). When the archive is ready, the
reaper deletes the sandbox. It deletes each sandbox archive after 30 days.

The plugin does not set `shutdownTime`. With `shutdownPolicy: Retain`, the
controller only stops the pod at that time, and the idle reaper of Hermes
already does that. The controller cannot take an archive before a delete, so
the plugin must do the delete in any case.

## Considered Options

- `shutdownTime` with `shutdownPolicy: Delete`: the controller deletes the
  Sandbox and its volume with no archive.
- A CronJob in home-ops: it needs its own account and image, and it repeats
  the rules of the plugin.

## Consequences

- The reaper runs only while Hermes runs. A sandbox expires late when Hermes
  is down, never early.
- The reaper never touches a running sandbox. It reads each sandbox again
  before it acts, so a command that resumes the sandbox wins.
- When the VolumeSnapshot quota is full, the archive fails and the sandbox
  stays until the next run.
- Each new sandbox has the annotations `hermes-k8s/task` and
  `hermes-k8s/profile`, so a person can find the task of a sandbox.
