# hermes-k8s

hermes-k8s gives each Hermes session its own isolated shell in the home
cluster, and it makes the images for Hermes and for that shell.

## Language

### Hermes

**Session**:
One conversation in Hermes, with its own history. The history stays in
Hermes when the sandbox of the session is deleted.
_Avoid_: chat, task, run

**Profile**:
A Hermes configuration with its own settings, secrets and model provider.
Many sessions can use one profile.
_Avoid_: user, account, tenant

**Turn**:
One request to the model and its answer. A turn can contain tool calls,
which Hermes runs in the sandbox.
_Avoid_: step, message

### Sandbox

**Sandbox**:
The isolated pod and its volume that run the shell commands of one session.
_Avoid_: container, runner, workspace pod, environment

**Suspended sandbox**:
A sandbox that has its volume but no pod. The next command of its session
starts a pod again on the same volume.
_Avoid_: stopped, paused, sleeping

**Sandbox archive**:
A copy of the volume of a sandbox, kept for a limited time after the
sandbox is deleted.
_Avoid_: backup, snapshot

**Workspace**:
The directory in a sandbox where the agent clones and changes repositories.
_Avoid_: work dir, project dir

**Nix base**:
The shared, prebuilt Nix store from which each new sandbox starts. Each
Nix base belongs to one version of the sandbox image.
_Avoid_: golden image, template volume, base snapshot

**Nix cache**:
The binary cache in the cluster from which a sandbox downloads Nix packages
that are not in its Nix base.
_Avoid_: substituter, mirror

**Service allowance**:
A permission that lets the sandboxes of a profile reach one more service.
_Avoid_: exception, whitelist

### Images

**Hermes image**:
The image that runs Hermes, its Dashboard and its Webapp, the `claude` CLI
and the plugins.
_Avoid_: agent image, gateway image

**Sandbox image**:
The image that a sandbox runs. It has the shared tools, Nix and devenv.
_Avoid_: agent image, runner image

### Model provider

**Subscription**:
One Claude Pro plan of the user, identified by its OAuth token.
_Avoid_: account, key, seat
