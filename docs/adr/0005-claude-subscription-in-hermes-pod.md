---
status: partly superseded by ADR-0010
---

# One Claude Pro subscription, used only in the Hermes pod

ADR 0010 replaces the sentence "`extra_usage` is `stop`" below. `extra_usage`
is `notify`. The rest of this ADR holds.

Hermes uses the Claude Pro subscription through the official plugin
`claude-subscription-directsdk`. For each turn, the plugin starts the
unmodified `claude` CLI in the Hermes pod with its tools turned off. The
tool calls of the model come back to Hermes, and Hermes runs them in the
sandbox. Thus the sandbox does not need `claude`, and the OAuth token
stays out of the sandbox.

We use one subscription. `extra_usage` is `stop`, so the plan never bills
overage. LiteLLM is the fallback model.

## Considered Options

- Rotation over more than one subscription when one reaches its limit: the
  plugin does not support it, and it goes around the limits that assume
  "ordinary, individual usage" (Claude Code legal and compliance page).
- One subscription for each profile: the plugin supports it, but we do not
  need it now.
- `claude` in the sandbox, for a nested Claude Code agent: the token must
  then go into the sandbox, where each command can read it.
