---
name: my
description: Check and set the agent's own runtime state (model, iterations, context window, token usage, web config). Use when diagnosing why something doesn't work ("why can't you search the web?", "why did you stop?"), checking resource limits before complex tasks, adapting configuration for long or simple tasks, or remembering user preferences across turns. Also use when the user asks what model you are running, how many tokens you've used, or what your settings are.
always: true
---

# Self-Awareness

## How to use

1. **Identify the situation** from the categories below
2. **Call the my tool** with the appropriate action
3. **If set**, warn the user before changing impactful settings (model, iterations)
4. **For detailed examples**, read [references/examples.md](references/examples.md)

## When to check

<rule>
**Diagnose before explaining.** When something doesn't work, check your state first.
</rule>

<rule>
**Check budget before complex tasks.** Know your limits before committing.
</rule>

<rule>
**Recall across turns.** Store preferences in your scratchpad, read them back later.
</rule>

## When to set

<rule>
**Only set when benefit is clear and user is informed.** Warn before changing model.
</rule>

| Situation | Command |
|-----------|---------|
| Large codebase analysis | `my(action="set", key="context_window_tokens", value=262144)` |
| Switch to a named model preset | `my(action="set", key="model_preset", value="<preset-name>")` |
| Repetitive simple tasks without a preset | `my(action="set", key="model", value="<fast-model>")` |
| Long multi-step task | `my(action="set", key="max_iterations", value=80)` |

**Tradeoff:** Bias toward stability. Only set when defaults are genuinely insufficient.

## Anti-patterns

<rule>
**Don't check every turn.** Costs a tool call. Use when you need information, not reflexively.
</rule>

<rule>
**Don't store sensitive data.** No API keys, passwords, or tokens in scratchpad. Sensitive-named keys and secret-shaped values are redacted from output and audit logs, but the raw value may still be stored in memory.
</rule>

<rule>
**Don't set workspace.** Does not update file tool boundaries — won't work.
</rule>

## Constraints

- All modifications in-memory only — restart resets everything
- Scratchpad is scoped per session; other chats cannot read or write your keys
- Per-value size cap 8 KB; per-session key cap 64; benign long strings may be redacted as `<redacted>`
- Prefer `model_preset` for configured model choices. Direct `model` changes clear the active preset and should only be used when no preset exists.
- Protected params have type/range validation: `max_iterations` (1–100), `context_window_tokens` (4096–1M), `model` (non-empty str)
- If `tools.my.allow_set` is false, check only
- If `tools.my.allow_scratchpad` is true (and `allow_set` is false), scratchpad writes are allowed but model/iteration/preset changes are still blocked
- If `tools.my.require_context` is true, writes/reads without a bound session return an error instead of using the shared `__global__` scope

## Related tools

| Need | Use | Persists? |
|------|-----|-----------|
| Per-session temp state | `my(action="set", key="...", value=...)` | No |
| Long-term facts | Memory skill (`MEMORY.md`, `USER.md`) | Yes |
| Permanent config change | Edit config file | Yes |

**Rule of thumb:** Tomorrow? Memory. This turn only? My.
