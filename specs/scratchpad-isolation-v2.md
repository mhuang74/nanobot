# Spec: Scratchpad Session Isolation & Hardening — v2

**Status:** Plan (not yet implemented)
**Owner:** —
**Last reviewed:** 2026-06-27
**Base:** `specs/scratchpad-isolation.md` (v1)

---

## Changes from v1

This revision incorporates a security-risk review that uncovered **high-severity gaps** in the original plan:

1. **Tool-output secret leakage** (HIGH) — v1 redacts audit logs but leaves secrets in chat-visible tool return strings.
2. **Unbounded session keys = memory DoS** (HIGH) — v1 adds per-session scoping but no cap on distinct sessions.
3. **`__global__` as a shared backdoor** (MEDIUM) — v1 documents it but does not offer a production kill-switch.
4. **Secret regex misses AWS, JWT, PEM, Basic auth** (MEDIUM).
5. **Migration TOCTOU on `_inspect` read paths** (LOW).
6. **Missing security boundary in `.agent/security.md`** (MEDIUM).
7. **No operational kill-switch / metrics / rollback** (MEDIUM).
8. **Subagent context propagation remains unresolved** (v1 Open Question #1).

---

## Problem (v1 findings, unchanged)

See v1 spec §Problem. All 6 original issues remain valid.

---

## Scope

**In scope (new):**
- `nanobot/agent/tools/self.py` — core fixes + output redaction
- `nanobot/agent/loop.py` — init, lock, session-cap, TTL tie-in
- `nanobot/agent/tools/runtime_state.py` — protocol update
- `tests/agent/tools/test_self_tool.py` — v1 test classes + output-redaction tests
- `docs/my-tool.md`, `nanobot/skills/my/SKILL.md` — correctness pass
- `.agent/security.md` — new "Runtime State Isolation" boundary section
- `nanobot/config/schema.py` — new `require_context` and `session_isolation` flags

**Out of scope (unchanged):**
- Persistent scratchpad across restarts.
- Encryption at rest.
- Per-user (vs per-session) scoping.

---

## Design Decisions

### D1–D7: Retained from v1
Session-namespace shape, `current_request_context()`, `__global__` fallback, 8 KB value cap, `asyncio.Lock`, audit redaction, and auto-migration are all preserved.

### D8. Output redaction (NEW)
Tool return strings that include `value!r` must pass through a `_redact_value(key, value)` helper so secrets never reach chat channels or LLM context.

Applies to:
- `_modify_scratchpad_only` return value
- `_modify_free` return value (both `setattr` and scratchpad branches)
- `_modify_restricted` return value
- `_inspect` / `check` return values (indirectly via `_format_value`, but needs redaction for secret-shaped values and sensitive keys)

**Tradeoff:** Benign long strings matching the regex will appear as `<redacted>` in chat. This is acceptable for security; document it in `docs/my-tool.md`.

### D9. Per-value size cap — 8 KB (unchanged from v1)
Serialize with `json.dumps(value, default=str, ensure_ascii=False)`; reject if `len(encoded) > 8192`.

### D10. Count-cap atomicity — `asyncio.Lock` (unchanged from v1)
Lock held only around read-count-check-then-write. Modify helpers become `async def`.

### D11. Session-cap / DoS prevention (NEW)
Add `_MAX_SESSIONS = 1024` on `AgentLoop`. Outer dict eviction uses LRU: when the cap is exceeded and a new session writes, evict the least-recently-read/written session.

**Alternative considered:** Tie scratchpad lifetime to `SessionManager` TTL. This is cleaner but couples the agent loop to session expiry in a way that may surprise operators ("my scratchpad disappeared after 24h"). LRU keeps the in-memory-only contract while bounding memory.

**Open question:** Should `_MAX_SESSIONS` be configurable via `tools.my.max_sessions`?

### D12. `__global__` hardening (NEW)
Add config flag:

```python
# nanobot/agent/tools/self.py
class MyToolConfig(Base):
    enable: bool = True
    allow_set: bool = False
    allow_scratchpad: bool = False
    require_context: bool = False   # NEW
```

When `require_context=True`:
- If `current_request_context()` returns `None` or a falsy `session_key`, writes return `"Error: no session context available"` instead of falling back to `__global__`.
- Reads return `"Error: no session context available"` or omit the scratchpad section.

Default `False` preserves backward compatibility. Production deployments should set `True`.

### D13. `session_isolation` kill-switch (NEW)
Add config flag `session_isolation: bool = True` on `MyToolConfig`.

When `False`:
- Revert to flat `_runtime_vars: dict[str, Any]` behavior (pre-v1).
- Used for emergency rollback if the new scoping causes unexpected regressions.

This requires the code path to support both shapes or to wrap the flat dict under `__global__` and always use `_session_scope`. The latter is simpler: `session_isolation=False` simply means all `_session_scope` calls return the `__global__` bucket regardless of context.

### D14. Secret regex expansion (NEW)
Replace v1 `_SECRET_VALUE_RE` with:

```python
_SECRET_VALUE_RE = re.compile(
    r"^(sk-[A-Za-z0-9]{20,}|"
    r"Bearer\s+[A-Za-z0-9_\-\.]{10,}|"
    r"xox[bpoar]-[A-Za-z0-9\-]+|"
    r"gh[ps]_[A-Za-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"ASIA[0-9A-Z]{16}|"
    r"Basic\s+[A-Za-z0-9+/=]{10,}|"
    r"eyJ[A-Za-z0-9_-]*\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*|"
    r"-----BEGIN\s+(RSA\s+|OPENSSH\s+|EC\s+|DSA\s+)?PRIVATE KEY-----|"
    r"[A-Fa-f0-9]{64}|"  # hex-64, e.g. Ethereum key
    r"[A-Za-z0-9_-]{40,}$)"  # catch-all long token
)
```

**Tradeoff:** `[A-Za-z0-9_-]{40,}` catches many benign tokens (hashes, UUIDs without dashes). Acceptable for audit logs; if applied to chat output (D8), operators may get complaints. Consider adding a `redact_output` flag separate from `redact_audit`.

### D15. Migration safety (NEW)
Move eager migration into `AgentLoop.__init__` instead of lazy `_session_scope`:

```python
# loop.py __init__
self._runtime_vars: dict[str, dict[str, Any]] = {}
self._runtime_vars_lock = asyncio.Lock()
```

Lazy migration is removed. On first deploy, existing in-memory flat dicts are lost (process restart anyway). This eliminates the TOCTOU entirely and is acceptable because `_runtime_vars` is in-memory-only.

If you strongly prefer zero-downtime migration, keep lazy migration but guard with a `bool` flag under the lock.

### D16. Security boundary documentation (NEW)
Add to `.agent/security.md`:

```markdown
## Runtime State Isolation

- `AgentLoop._runtime_vars` (the agent scratchpad) is namespaced by session key.
- The `my` tool is the sole writer.
- `__global__` is a shared fallback scope for CLI/test usage.
- Per-value size cap: 8 KB. Per-session key cap: 64. Global session cap: 1024.
```

### D17. Subagent context propagation (RESOLVED)
**Decision:** Before shipping, verify whether `nanobot/agent/subagent.py` executes `my` tool calls through the parent `AgentLoop` or a separate instance.

- **If through parent loop:** Subagent inherits the parent's `RequestContext` automatically (same `bind_request_context` lifetime). No code change needed; add a test to `TestScratchpadIsolation` confirming this.
- **If through separate loop:** Subagent must call `bind_request_context` with the parent's `session_key`. Add a requirement to the subagent runner.

---

## Implementation Steps (revised)

### Step 1 — Config schema (`nanobot/config/schema.py` + `self.py`)
Add `require_context: bool = False` and `session_isolation: bool = True` to `MyToolConfig`.

### Step 2 — Session-scope helper + eager init (`self.py`, `loop.py`)
Add `GLOBAL_SESSION_KEY`, `_session_scope()`, `_check_value_size()`, `_redact_value()`.
Update `AgentLoop.__init__` with nested dict + lock. Remove lazy migration.

### Step 3 — Make modify helpers async + locking (`self.py`)
Convert `_modify_scratchpad_only`, `_modify_free` to `async def`. Wrap count-write in `async with lock`.

### Step 4 — Output redaction (`self.py`)
Apply `_redact_value()` to all return strings in `_modify_*`. Apply to `_inspect` / `_format_value` for sensitive keys and secret-shaped values.

### Step 5 — Session cap (`loop.py`)
Add `_MAX_SESSIONS`, LRU eviction on outer dict overflow. Add `scratchpad_session_count` metric log on eviction.

### Step 6 — Inspect paths scoped (`self.py`)
Same as v1 Step 4, plus redaction.

### Step 7 — Audit redaction (`self.py`)
Same as v1 Step 5, with expanded regex (D14).

### Step 8 — Protocol update (`runtime_state.py`)
Nested dict type + lock.

### Step 9 — Security boundary doc (`.agent/security.md`)
D16.

### Step 10 — Tests (`test_self_tool.py`)
Same v1 test classes A–G, plus:
- **`TestOutputRedaction`**:
  - `test_sensitive_key_output_redacted` — verify chat string contains `<redacted>`
  - `test_secret_value_output_redacted` — verify chat string contains `<redacted>`
  - `test_benign_value_output_plain` — verify chat string contains literal value
  - `test_inspect_redacts_sensitive_keys`
  - `test_inspect_redacts_secret_values`

### Step 11 — Subagent verification
Code trace + test for subagent session scope.

### Step 12 — Verification checklist
1. `pytest tests/agent/tools/test_self_tool.py -v`
2. `ruff check ...`
3. `rg "per-session" docs/ nanobot/skills/` — claims accurate
4. Manual smoke with two WebSocket sessions

---

## Rollout / Backward Compatibility

- `_runtime_vars` shape change is in-memory only; no on-disk migration.
- `session_isolation=True` (default) changes behavior for multi-session deployments.
- `require_context=False` (default) preserves CLI/test compatibility.
- Config kill-switch `session_isolation=False` allows instant rollback to flat dict.

---

## Risks of This Change (revised)

| Risk | Mitigation |
|------|------------|
| Subagent context propagation | Step 11 verification required before ship |
| Lock contention under heavy load | Critical section is tiny; `_MAX_SESSIONS` bounds outer dict; add per-session locks later if profiled |
| Audit/debuggability loss from redaction | Non-sensitive values still logged; config flags allow tuning |
| Benign long strings redacted in chat | Documented tradeoff; `redact_output` could be split from `redact_audit` if needed |
| Memory DoS from unbounded sessions | `_MAX_SESSIONS` + LRU eviction |

---

## Open Questions (to resolve during implementation)

1. **Should `_MAX_SESSIONS` be configurable?** 1024 is the proposed hardcoded default.
2. **Should output redaction and audit redaction share the same regex?** A separate `redact_chat_output` flag would reduce false-positive chat noise.
3. **Do you prefer eager migration (lose legacy data on restart) or lazy migration with a lock-guarded flag?** Eager is simpler; lazy preserves in-flight data.
4. **Subagent path:** Does subagent execution flow through the parent `AgentLoop` or a separate instance?
