# Spec: Scratchpad Session Isolation & Hardening

**Status:** Plan (not yet implemented)
**Owner:** —
**Last reviewed:** 2026-06-27

## Problem

`MyTool` scratchpad (writes to `AgentLoop._runtime_vars`) was added so the agent
can persist notes across turns without modifying runtime state. A risk review
of `nanobot/agent/tools/self.py` found:

1. **Cross-session data leakage (HIGH).** `_runtime_vars` is a single
   process-global dict on the loop instance
   ([`loop.py:340`](../nanobot/agent/loop.py)) shared by all sessions/chats.
   - `set_context` stores channel/chat_id only for audit logging
     ([`self.py:135-137`](../nanobot/agent/tools/self.py)) — they do **not**
     scope storage.
   - Writes go to the shared dict
     ([`self.py:433`](../nanobot/agent/tools/self.py),
     [`self.py:539`](../nanobot/agent/tools/self.py)).
   - User A stores a value under any name → User B reads it via
     `check <key>` ([`self.py:360`](../nanobot/agent/tools/self.py)),
     `check scratchpad` ([`self.py:356`](../nanobot/agent/tools/self.py)),
     or the no-arg overview `check`
     ([`self.py:385-388`](../nanobot/agent/tools/self.py)).
   - User B can overwrite/delete User A's keys.
   - `docs/my-tool.md` and `nanobot/skills/my/SKILL.md` advertise
     "per-session" — this is inaccurate today.

2. **Context race.** `MyTool` is a single instance registered once per loop
   ([`loop.py:507`](../nanobot/agent/loop.py)). `set_context` overwrites plain
   instance attrs `_channel`/`_chat_id`
   ([`self.py:121-122`](../nanobot/agent/tools/self.py)) per request. Concurrent
   sessions overwrite each other's context before `execute` runs. Sibling tools
   (`message.py`, `cron.py`, `spawn.py`) use `ContextVar`; `MyTool` does not.

3. **No per-value size cap.** Only key *count* is bounded
   (`_MAX_RUNTIME_KEYS = 64`). A single value can be an arbitrarily large
   string/list/dict; only nesting depth ≤10 is checked
   ([`self.py:544-561`](../nanobot/agent/tools/self.py)). Unbounded memory
   growth on a shared dict.

4. **TOCTOU on count cap.** `len(rv) >= 64` then `rv[key] = value`
   ([`self.py:429-433`](../nanobot/agent/tools/self.py)) is check-then-set
   without a lock — two concurrent sessions can both pass the check and exceed
   64. The GIL makes per-op dict mutation safe, but the count cap is defeated.

5. **Audit log may leak secrets.** `_audit` logs `value!r`
   ([`self.py:434`](../nanobot/agent/tools/self.py),
   [`self.py:540`](../nanobot/agent/tools/self.py)). If a secret is stored under
   a non-sensitive name (the sensitive-name heuristic
   [`self.py:98-102`](../nanobot/agent/tools/self.py) only matches exact
   underscore splits: `apikey`, `tok`, etc. slip through), the secret lands in
   the info log.

6. **Missing tests** for isolation, size, concurrency, context race, and
   audit redaction. `TestScratchpadOnlyMode` uses a single mock loop and never
   asserts two sessions cannot read each other's keys — the gap that let the
   "per-session" claim ship.

## Scope

**Files changed:**
- `nanobot/agent/tools/self.py` — core fixes (session scoping, size cap,
  lock, audit redaction, context-source switch)
- `nanobot/agent/loop.py` — initialize new `_runtime_vars` shape and lock
- `nanobot/agent/tools/runtime_state.py` — protocol update
- `tests/agent/tools/test_self_tool.py` — new test classes
- `docs/my-tool.md`, `nanobot/skills/my/SKILL.md` — correctness pass

**Out of scope:**
- Persistent scratchpad across restarts (current contract: in-memory only).
- Encryption at rest.
- Per-user (vs per-session) scoping — current model is per-chat.
- Other consumers of `_runtime_vars` — none exist today; scratchpad is the
  only writer.
- Backfilling the `scratchpad` alias in `_inspect` for cross-session admin
  views (a future "admin inspect all sessions" tool, if needed, is a separate
  spec).

## Design Decisions

These were confirmed during planning:

### D1. Storage shape — namespaced by session
Change `_runtime_vars: dict[str, Any]` →
`_runtime_vars: dict[str, dict[str, Any]]` where the outer key is
`session_key` (e.g. `"telegram:12345"`, or `UNIFIED_SESSION_KEY` when
unified session mode is on).

Inner dict (per-session) is created lazily on first write.

### D2. Context acquisition — `current_request_context()`
Read `current_request_context()` from
[`nanobot/agent/tools/context.py`](../nanobot/agent/tools/context.py) at
execute time to obtain `session_key`. This avoids duplicating `ContextVar`
machinery in `MyTool` (the request context already flows through
`bind_request_context` in the runner) and is consistent with how other
context-aware behavior is reached.

Keep `_channel` / `_chat_id` only as an audit-log fallback when no context is
bound (e.g. direct CLI invocation without a request context). They must not
drive storage scope.

### D3. Fallback scope when no context
Introduce `GLOBAL_SESSION_KEY = "__global__"` in `self.py`. When no
`RequestContext` is bound (or `ctx.session_key` is falsy), writes/reads go to
the `__global__` scope. This is shared (no isolation) and documented as such —
it covers test fixtures and CLI usage without forcing every call site to bind
context.

### D4. Per-value size cap — 8 KB
Add `_MAX_VALUE_BYTES = 8192`. Serialize the candidate value with
`json.dumps(value, default=str, ensure_ascii=False)` and compare
`len(encoded)`. Reject if over the cap. Checked in both `_modify_scratchpad_only`
and the free-var branch of `_modify_free`.

8 KB is plenty for notes/preferences and blocks accidental large blobs /
pasted documents.

### D5. Count-cap atomicity — `asyncio.Lock`
Add `self._runtime_vars_lock: asyncio.Lock` on the loop
(`AgentLoop.__init__`, near
[`loop.py:340`](../nanobot/agent/loop.py)). Hold the lock only around the
read-count-check-then-write sequence, not around formatting or audit logging.
Both `_modify_scratchpad_only` and `_modify_free` acquire it.

`MyTool.execute` is already `async`, so the modify helpers can be made
`async` (currently sync — they are called from `async execute` which is fine).
Change `_modify_scratchpad_only` and `_modify_free` to `async def` and use
`async with runtime_state._runtime_vars_lock:`.

### D6. Audit redaction
In `_audit(action, detail)`, keep the current call signature but change
callers to pass `(key, value)` separately so redaction can decide:

```python
def _audit(self, action: str, key: str, value: Any | None = None,
           *, redact: bool = False) -> None:
    shown = "<redacted>" if redact else repr(value)
    ...
```

Redact when:
- `_is_sensitive_field_name(key)` is true, OR
- `value` matches a secret-shaped regex (e.g. `sk-`, `Bearer `, `xoxb-`,
  20+ char hex/base64). Compile the regex once at module level:
  `_SECRET_VALUE_RE = re.compile(r"^(sk-|Bearer |xox[bpoa]-|[A-Za-z0-9_-]{20,}$)")`.

Non-sensitive values (e.g. `42`, `True`, `"nanobot"`) continue to be logged
verbatim for debugging.

### D7. Legacy migration — auto on first access
On the first call to the new `_session_scope(runtime_state)` helper, detect a
legacy flat dict (any value is not a `dict`) and wrap it:

```python
if rv and not all(isinstance(v, dict) for v in rv.values()):
    runtime_state._runtime_vars = {GLOBAL_SESSION_KEY: dict(rv)}
    rv = runtime_state._runtime_vars
```

Performed once transparently; subsequent calls see the nested shape. No restart
required. Existing scratchpad contents survive under `__global__`.

## Implementation Steps

### Step 1 — Session-scope helper (`self.py`)

Add module-level constant and helper:

```python
GLOBAL_SESSION_KEY = "__global__"

def _session_scope(runtime_state) -> dict[str, Any]:
    ctx = current_request_context()
    key = ctx.session_key if ctx and ctx.session_key else GLOBAL_SESSION_KEY
    rv = runtime_state._runtime_vars
    # D7: migrate legacy flat shape once
    if rv and not all(isinstance(v, dict) for v in rv.values()):
        runtime_state._runtime_vars = {GLOBAL_SESSION_KEY: dict(rv)}
        rv = runtime_state._runtime_vars
    return rv.setdefault(key, {})
```

Import `current_request_context` from `nanobot.agent.tools.context`.

### Step 2 — Make modify helpers async + locking

Convert `MyTool._modify_scratchpad_only` and `MyTool._modify_free` to
`async def`. Wrap the count-check + write in:

```python
async with self._runtime_state._runtime_vars_lock:
    scoped = _session_scope(self._runtime_state)
    if key not in scoped and len(scoped) >= self._MAX_RUNTIME_KEYS:
        ... reject
    old = scoped.get(key)
    scoped[key] = value
```

Update `execute` to `await self._modify_scratchpad_only(...)` /
`await self._modify_free(...)`.

### Step 3 — Size cap helper (`self.py`)

```python
_MAX_VALUE_BYTES = 8192

@staticmethod
def _check_value_size(value: Any) -> str | None:
    try:
        encoded = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return f"value not serializable: {e}"
    if len(encoded) > MyTool._MAX_VALUE_BYTES:
        return (f"value exceeds {MyTool._MAX_VALUE_BYTES} bytes "
                f"(got {len(encoded)})")
    return None
```

Call before the count check in both modify paths. Order:
1. `_validate_key`
2. dot-path / blocked / read-only / restricted / real-attr guards (existing)
3. callable rejection (existing)
4. `_validate_json_safe` (existing)
5. **NEW** `_check_value_size`
6. count cap (locked)
7. write (locked)

### Step 4 — Rewrite inspect paths to use scoped dict

`_inspect(key)`:
- `key == "scratchpad"` → `_session_scope` + `_format_value(scoped, "scratchpad")`.
- Lookups for simple keys: check scoped dict, not flat `_runtime_vars`.
- Fallback to `__global__` if not found in caller's scope? **No** — isolation
  requires that a session not see another's keys. Only look in the caller's
  scope (or `__global__` if that *is* the caller's scope).

`_inspect_all()`:
- Append `_format_value(scoped, "scratchpad")` — current session only.

### Step 5 — Audit redaction (`self.py`)

Add module-level:

```python
import re
_SECRET_VALUE_RE = re.compile(
    r"^(sk-|Bearer |xox[bpoa]-|gh[ps]_|AIza[0-9A-Za-z_-]{30,}|[A-Za-z0-9_-]{32,}$)"
)
```

Refactor `_audit`:

```python
def _audit(self, action: str, detail: str) -> None:
    session = (f"{self._channel}:{self._chat_id}"
               if self._channel else "unknown")
    logger.info("self.{} | {} | session:{}", action, detail, session)
```

Keep the `detail` string built by callers, but in callers use a
`_redact_value(key, value)` helper to decide whether to show `value!r` or
`<redacted>`:

```python
def _redact_value(self, key: str, value: Any) -> str:
    if self._is_sensitive_field_name(key):
        return "<redacted>"
    if isinstance(value, str) and _SECRET_VALUE_RE.match(value):
        return "<redacted>"
    return repr(value)
```

Apply in `_modify_scratchpad_only`, `_modify_free`, and `_modify_restricted`
(details that include `value!r`).

### Step 6 — AgentLoop init (`loop.py:~340`)

Replace:

```python
self._runtime_vars: dict[str, Any] = {}
```

with:

```python
self._runtime_vars: dict[str, dict[str, Any]] = {}
self._runtime_vars_lock = asyncio.Lock()
```

Ensure `asyncio` is imported (it is, throughout the file).

### Step 7 — Protocol update (`runtime_state.py`)

Update `_runtime_vars` typing to `dict[str, dict[str, Any]]` and add
`_runtime_vars_lock: asyncio.Lock`. Mirror in any mock fixtures
(`tests/agent/tools/test_self_tool.py:_make_mock_loop`).

### Step 8 — Tests (`tests/agent/tools/test_self_tool.py`)

Update the `_make_mock_loop` helper:
- `loop._runtime_vars = {}` (new shape — wait, this is `{}` which is the
  empty nested dict; that's fine, lazily populated).
- `loop._runtime_vars_lock = asyncio.Lock()` (real lock, not a MagicMock —
  required for `async with`).

Add a `bind_request_context` / `reset_request_context` helper for tests that
need to simulate a session. See `nanobot/agent/tools/context.py`.

New test classes:

#### A. `TestScratchpadIsolation`
- `test_two_sessions_cannot_read_each_other_keys`
  Bind ctx A (`channel="telegram", chat_id="1"`, session_key="telegram:1").
  `set key=foo value=bar`. Bind ctx B (`session_key="telegram:2"`).
  `check foo` → not found / "scratchpad is empty".
- `test_two_sessions_cannot_write_each_other_keys`
  A sets `k=v1`. B sets `k=v2`. A `check k` → `v1` (unchanged).
- `test_no_context_falls_back_to_global`
  No `RequestContext` bound. `set k=v` succeeds; `check k` returns `v`.
- `test_no_arg_check_only_returns_current_session`
  A writes 1 key, B writes 1 key. Under ctx A: `check` (no key) → B's key
  absent from the "scratchpad:" section.
- `test_check_scratchpad_alias_scoped`
  Same as above but via `check scratchpad`.
- `test_unified_session_shares_scope`
  Both sessions use `_unified_session=True` → `session_key_for_channel`
  returns `UNIFIED_SESSION_KEY` → they share one scope (document expected,
  assert both see the same key).
- `test_subagent_inherits_parent_session_scope`
  If subagent path carries the parent's `RequestContext` (verify via
  `bind_request_context` propagation), a scratchpad write from the subagent
  lands in the parent's scope. If subagents do not bind context, they write
  to `__global__` — document whichever the code confirms.

#### B. `TestScratchpadMigration`
- `test_legacy_flat_dict_wrapped_under_global`
  Preload `loop._runtime_vars = {"k": 1}` (flat). First `check k` (no ctx)
  → returns `1`; assert `loop._runtime_vars` is now
  `{"__global__": {"k": 1}}`.
- `test_migrated_global_then_request_scoped_writes_separate`
  After migration, bind ctx A and write `k2`. Assert `__global__` still
  has only `k=1`; A's scope has `k2`.

#### C. `TestValueSizeCap`
- `test_oversized_string_rejected`
  `value = "x" * 9000` → error mentions `8192 bytes`.
- `test_oversized_dict_rejected`
  Dict with nested content serializing > 8192 bytes.
- `test_at_limit_allowed`
  Construct a string whose JSON form is exactly 8192 bytes → OK.
- `test_size_cap_applies_in_allow_set_path`
  Same oversized value with `modify_allowed=True`.
- `test_callable_rejected_before_size_check`
  Ordering preserved: callable rejected before size check runs.
- `test_non_serializable_rejected`
  Value with a non-JSON-serializable nested object (e.g. a `set`) →
  `_validate_json_safe` catches first; confirm size helper also reports
  "not serializable" if reached.

#### D. `TestCountCapConcurrency`
- `test_max_keys_per_session`
  Write 64 distinct keys under one session → 65th rejected. Count is
  **per-session**, not global.
- `test_max_keys_independent_per_session`
  A fills 64. B can still write 64.
- `test_concurrent_writes_do_not_exceed_cap`
  `await asyncio.gather(*[tool.execute("set", key=f"k{i}", value=i)
  for i in range(100)])` under one session. Assert exactly 64 keys stored,
  others rejected, no exception raised.

#### E. `TestContextRace`
- `test_concurrent_sessions_keep_own_session_key`
  Use `bind_request_context` to set ctx A, start a `tool.execute("set",...)`
  coroutine but pause before the write (use an event). Switch to ctx B,
  run a `check` — it must reflect B's scope, not A's. Then resume A. This
  proves `current_request_context()` is read at execute time and not
  captured stale on `self`.

If `current_request_context()` is a `ContextVar`, this naturally works per
async task. The test confirms the implementation didn't regress to instance
attrs.

#### F. `TestAuditRedaction`
- Capture loguru sink (configure a list-backed sink in a fixture).
- `test_audit_redacts_sensitive_named_key`
  `set api_key="sk-abc"` is *blocked* by `_SENSITIVE_NAMES` (value not
  stored) — but assert the audit log for the block shows the *attempt* with
  redaction OR omits the value entirely.
- `test_audit_redacts_secret_shaped_value`
  `set my_token="sk-abc123-longtoken"` — key `my_token` is not in
  `_SENSITIVE_NAMES` (not an exact match; `token` is, so this might pass…
  pick `my_credential` — no; `credential` is in the set. Use `foobar` with
  a value matching `_SECRET_VALUE_RE`). Assert log line shows
  `<redacted>`.
- `test_audit_non_sensitive_value_logged_plain`
  `set k=42` → log line contains `42`.

#### G. Regression — existing suites
- `TestScratchpadOnlyMode` and `TestRuntimeVarsInspectFallback` must pass
  with minimal updates. They do not bind a `RequestContext` → they resolve
  to `__global__` → behavior unchanged. Update assertions that compared
  against `loop._runtime_vars[key]` directly to use
  `loop._runtime_vars["__global__"][key]`.

## Verification

1. `pytest tests/agent/tools/test_self_tool.py -v` — all green.
2. `ruff check nanobot/agent/tools/self.py nanobot/agent/loop.py
   nanobot/agent/tools/runtime_state.py` — clean.
3. `rg "per-session" docs/ nanobot/skills/` — confirm docs now accurately
   describe per-session behavior (or document the `__global__` fallback).
4. Manual smoke: `nanobot gateway` with two WebSocket sessions; write
   `set k=fromA` in session 1, `check k` in session 2 → not found; `check`
   (no key) in session 2 → scratchpad section does not show session 1's
   key.

## Rollout / Backward Compatibility

- `_runtime_vars` shape change is in-memory only; nothing persists across
  restart, so no on-disk migration needed.
- D7 auto-migration covers any in-flight process that upgrades while
  holding legacy flat scratchpad data.
- Config flags `tools.my.allow_set` and `tools.my.allow_scratchpad` are
  unchanged.
- No public API change: the `my` tool's parameters, actions, and return
  strings are unchanged (except: cross-session keys no longer leak, and
  oversized writes now error).

## Risks of This Change

- **Subagent context propagation.** If subagent runs do not
  `bind_request_context`, their scratchpad writes land in `__global__`,
  not the parent session. Need to verify before shipping; if broken, add a
  follow-up to bind context in the subagent runner.
- **Lock contention.** `_runtime_vars_lock` is loop-global. Under heavy
  multi-session load, concurrent `set` calls serialize on it. The critical
  section is tiny (dict get + len + set), so acceptable. If profiling later
  shows contention, move to per-session locks in a
  `dict[str, asyncio.Lock]`.
- **Audit verbosity.** Redaction reduces debuggability for genuinely
  sensitive data. Acceptable trade; non-sensitive values still logged.

## Open Questions (to resolve during implementation)

1. Does `nanobot/agent/subagent.py` (or the runner that executes subagent
   turns) call `bind_request_context` with the parent's session key? If
   not, Step 4 / tests need adjustment and a follow-up ticket.
2. Are there any other call sites that read `AgentLoop._runtime_vars`
   directly (e.g. for telemetry, persistence, debug)? `rg "_runtime_vars"`
   shows only `loop.py:340` (init) and `self.py` — confirm during impl.
