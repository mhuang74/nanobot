# Security Boundaries

The agent operates with significant power (file system, shell, web). The following guards must not be bypassed when modifying related code.

## Workspace Restriction

Filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `apply_patch`) resolve paths through the workspace path resolver (`agent/tools/filesystem.py` / `agent/tools/path_utils.py`), which enforces that the resolved path must lie under the active workspace when workspace restriction is enabled. The media upload directory is always an internal extra read root while restricted.

Additional filesystem roots must be capability-specific. `extra_allowed_dirs` is a legacy read-only alias. Use `extra_read_allowed_dirs` for read-only roots, `extra_write_allowed_dirs` only when a write-capable tool is intentionally allowed to modify an extra directory, and exact file allowlists when a tool may modify only specific files.

Shell execution (`ExecTool`, `agent/tools/shell.py`) also respects `restrict_to_workspace` as an application-level guard: if enabled and `working_dir` is outside the workspace, the command is rejected before execution, and command text is checked for obvious workspace escapes. This is not process-level isolation; use an exec sandbox backend for that.

**Rule**: Any new path-handling logic must go through the workspace path resolver or perform an equivalent containment check with explicit read/write capability semantics.

## SSRF Protection

All outbound HTTP requests from agent tools must pass through `validate_url_target` (`security/network.py`). By default it blocks loopback, RFC1918 private addresses, CGNAT ranges, link-local ranges, and cloud metadata endpoints (including `169.254.169.254`).

The only escape hatch is `configure_ssrf_whitelist(cidrs)`, which reads from `config.tools.ssrf_whitelist` at load time.

HTTP/SSE MCP transports are part of this boundary: validate configured MCP URLs before probing or constructing clients, and validate each outgoing HTTP request before redirects are followed. Local/private HTTP MCP endpoints are allowed only through the explicit SSRF whitelist. Stdio MCP servers are not part of the HTTP SSRF path.

**Rule**: Do not add direct `httpx.get` / `requests.get` calls in tools. Route through the existing web fetch utilities or replicate the `validate_url_target` check.

## Shell Sandbox

`tools/sandbox.py` provides optional command wrapping. The only backend currently shipped is `bwrap` (bubblewrap), intended for containerized deployments. On Windows and bare-metal Linux without `bwrap`, commands run in the native shell with workspace restriction as an application-level guard only.

**Rule**: If adding a new sandbox backend, implement `_wrap_<name>(command, workspace, cwd) -> str` and register it in `_BACKENDS`.

## Runtime State Isolation

`AgentLoop._runtime_vars` (the agent scratchpad) is namespaced by session key so one chat cannot read or write another's per-session notes.

- `_runtime_vars` is a nested dict: `dict[str, dict[str, Any]]` (outer key = session key, inner = per-session scratchpad keys).
- The `my` tool is the sole writer; it resolves the active scope via `current_request_context()` at execute time (ContextVar, not instance-captured), so concurrent turns cannot race on session scope.
- `__global__` is a shared fallback scope used by CLI/test paths when no `RequestContext` is bound. Set `tools.my.require_context: true` in production to reject the `__global__` fallback and surface "no session context available" errors instead.
- `tools.my.session_isolation: false` is an emergency kill-switch that flattens every session back onto `__global__` (pre-v1 behavior).
- Per-value size cap: 8 KB (JSON-encoded). Per-session key cap: 64. Global session cap: 1024 with LRU eviction (the `__global__` scope is never evicted).
- All count-check-then-write sequences are gated by `AgentLoop._runtime_vars_lock`.
- Tool return strings and audit logs pass through `_redact_value` / `_SECRET_VALUE_RE` so sensitive-named keys and secret-shaped values appear as `<redacted>` and never reach chat channels, LLM context, or info logs.

**Rule**: Any code that reads or writes `_runtime_vars` must go through the `my` tool's session-scope helpers (`_read_scope` / `_write_scope`) and never touch the nested dict directly.
