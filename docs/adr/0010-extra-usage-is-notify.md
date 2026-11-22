---
status: accepted
supersedes: ADR-0005 (the setting `extra_usage` only)
---

# The plugin setting `extra_usage` is `notify`, and the Anthropic account stops overage

This ADR supersedes [ADR 0005](0005-claude-subscription-in-hermes-pod.md) on
one point. ADR 0005 said that `extra_usage` is `stop`, so the plan never bills
overage. That statement is wrong for our token. The rest of ADR 0005 still holds.

The token is the result of `claude setup-token`. With this token, Claude Code
cannot read the plan limits. In the modes `ask` and `stop`, the plugin refuses
a request when Claude Code cannot say whether the account draws extra usage.
Thus `stop` refuses every turn. Spike 02 (vault note "Subscription turns in Hermes") shows this. A setting that refuses every turn is
not a safe default.

We set `extra_usage` to `notify`. The plugin lets each request continue and writes
one warning for each limit window to the log. The plugin does not stop overage. The
user must disable extra usage in the settings of the Anthropic account. That
account setting stops overage.

- We do not run `claude auth login` in the Hermes pod. A login that gives the
  plan limits needs a browser session. The login also writes credential files
  to the volume.
- The overlay of the Hermes pod sets `extra_usage`, so a save in the Dashboard
  cannot change it.

## Considered Options

- `stop` with a token from `claude setup-token`: it refuses every turn. We reject it.
- `ask` with a token from `claude setup-token`: it refuses each turn until the user
  allows the window, and it has the same problem. We reject it.
- `claude auth login` in the pod, with `stop`: the plugin can then read the
  plan limits. But the login needs an interactive browser step, and it writes
  credential files on the volume. We reject it for now.

## Consequences

- The request that crosses the plan limit can bill overage if extra usage is enabled
  in the Anthropic account. The user must keep extra usage off. This is a step
  of the user, and no code in this repository checks it.
- A turn can fail when the plan limit is reached. ADR 0005 names LiteLLM as
  the fallback model.
- The token expires. The user creates a new token before the date in the 1Password
  field `CLAUDE_CODE_OAUTH_TOKEN valid until`.
