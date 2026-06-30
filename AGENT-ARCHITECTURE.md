# nanobot Agent Architecture

A comprehensive reference for how the nanobot agent works — design principles, message flow, the agentic loop, prompt system, memory architecture, and context management.

---

## 1. Design Principles

The agent core is governed by five architectural constraints:

| Principle | What it means |
|-----------|--------------|
| **Core stays small** | `agent/loop.py` and `agent/runner.py` are the critical path. New capabilities belong at the edges: channels, tools, skills, or MCP servers. |
| **Less structure, more intelligence** | Prefer simple, readable code over new framework layers. Add structure only when it removes real complexity or protects an important boundary. |
| **Prefer duplication over premature abstraction** | Channel and provider implementations should remain self-contained. No complex base classes to eliminate cross-file duplication. |
| **Minimal change** | Bug fixes change only what is necessary. Formatting churn and unrelated cleanups never mix into functional PRs. |
| **Explicit over magical** | Configuration declared in Pydantic models. Error handling raises clear exceptions. All resolution paths are traceable. |

---

## 2. High-Level Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Channels   │────▶│  MessageBus  │────▶│  AgentLoop   │
│ (Telegram,   │◀────│ (in/out Qs)  │◀────│  (state      │
│  Discord,    │     └──────────────┘     │   machine)   │
│  Slack, …)   │                           └──────┬───────┘
└──────────────┘                                  │
                                                  ▼
                                         ┌────────────────┐
                                         │  AgentRunner   │
                                         │  (iteration    │
                                         │   loop)        │
                                         └──────┬─────────┘
                                                │
                    ┌───────────────────────────┼───────────────────────┐
                    │                           │                       │
                    ▼                           ▼                       ▼
            ┌──────────────┐          ┌──────────────┐         ┌──────────────┐
            │   Tools      │          │   Providers  │         │    Memory    │
            │ (filesystem, │          │ (OpenAI,     │         │ (session     │
            │  shell, web, │          │  Anthropic,  │         │  history,     │
            │  MCP, cron,  │          │  Bedrock, …) │         │  Dream,       │
            │  subagent)   │          └──────────────┘         │  compaction)  │
            └──────────────┘                                    └──────────────┘
```

### Message Bus

An async `MessageBus` (`nanobot/bus/queue.py`) decouples channels from the agent core:

- **Inbound queue**: Channels publish `InboundMessage(channel, sender_id, chat_id, content, media, metadata)`
- **Outbound queue**: Agent publishes `OutboundMessage(channel, chat_id, content, metadata)`
- Blocking `asyncio.Queue` semantics — channels and agent run independently

### Session Key Resolution

```
Effective key = UNIFIED_SESSION_KEY (if unified_session=True and no override)
              = msg.session_key (otherwise)
```

This determines per-session lock, pending queue, and task routing.

---

## 3. The Agentic Loop

### 3.1 AgentLoop — State Machine Turn Processing

`AgentLoop` (`nanobot/agent/loop.py`, ~1800 lines) is the core processing engine. It sits between the `MessageBus` and `AgentRunner`, orchestrating the entire turn lifecycle.

**Main event loop** — consumes inbound messages:

```python
while self._running:
    msg = await self.bus.consume_inbound()  # blocks until a message arrives
    # Handle runtime control messages, priority commands
    # Check for mid-turn injection (session already active)
    # Otherwise, create a new task for _dispatch()
```

**Per-session dispatch** — serializes messages within a session, runs concurrently across sessions:

```python
async with session_lock:
    async with global_semaphore:
        pending_queue = asyncio.Queue()  # for mid-turn injection
        await _process_message(msg)      # state machine
        bus.publish_outbound(response)
        drain leftover pending messages
```

**State machine transitions** — each state returns an event string that drives the next transition:

```
RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE
```

| State | Handler | Responsibility |
|-------|---------|---------------|
| `RESTORE` | `_state_restore` | Load session, extract document text, restore runtime checkpoint (crash recovery), restore pending user turn |
| `COMPACT` | `_state_compact` | Call `AutoCompact.prepare_session()` — may archive expired sessions and return a summary |
| `COMMAND` | `_state_command` | Dispatch slash commands; if handled, shortcut to DONE; otherwise continue to BUILD |
| `BUILD` | `_state_build` | Merge history, build system prompt + messages via `ContextBuilder`, persist user message early |
| `RUN` | `_state_run` | Call `AgentRunner.run()` — the actual LLM conversation with tool execution |
| `SAVE` | `_state_save` | Save turn messages to session, enforce file cap, schedule background consolidation |
| `RESPOND` | `_state_respond` | Assemble `OutboundMessage` from turn results |

### 3.2 AgentRunner — Iteration Loop

`AgentRunner` (`nanobot/agent/runner.py`, ~1500 lines) is the shared execution loop that runs tool-using LLM conversations. It is a pure agentic loop without product-layer concerns.

**Core iteration loop:**

```python
for iteration in range(spec.max_iterations):  # default 200
    # 1. Context governance: drop orphans, backfill, microcompact, snip history
    messages_for_model = self._prepare_messages(messages)

    # 2. Request model
    response = await self._request_model(spec, messages_for_model, hook, context)

    # 3. If model returned tool calls:
    #    - Append assistant message with tool calls
    #    - Execute tools (batched, concurrent or sequential)
    #    - Append tool results
    #    - Emit checkpoint
    #    - Drain mid-turn injections
    #    - Continue to next iteration

    # 4. If model returned final answer:
    #    - Handle empty response retries (up to 2)
    #    - Handle length recovery (up to 2)
    #    - Drain remaining injections
    #    - Break loop

    # 5. If max_iterations reached:
    #    - Drain remaining injections
    #    - Try finalize (budget-exhausted finalization)
    #    - Fallback message
```

**Key capabilities:**

- **Retry & recovery**: Empty response retries, length recovery, LLM error handling
- **Checkpoint emission**: Runtime state persisted to session metadata at key points for crash recovery and `/stop` handling
- **Context governance**: Micro-compaction, history snipping, orphan cleanup
- **Tool governance**: Concurrent execution of `concurrency_safe` tools via `asyncio.gather()`, safety boundary enforcement (SSRF, workspace violations)
- **Injection handling**: Mid-turn messages and subagent results picked up between iterations

### 3.3 End-to-End Message Flow

```
[Channel]
    │ InboundMessage(channel, sender_id, chat_id, content, media, metadata)
    ▼
MessageBus.inbound
    │
    ▼
AgentLoop.run()
    │ consume_inbound()
    ▼
_dispatch(msg)                          ← per-session lock, global semaphore
    │
    ▼
_process_message(msg)                  ← state machine
    │
    ├─ RESTORE ──▶ COMPACT ──▶ COMMAND ──▶ BUILD ──▶ RUN ──▶ SAVE ──▶ RESPOND
    │
    ▼
AgentRunner.run()                       ← iteration loop
    │
    ├─ _prepare_messages()              ← context governance
    ├─ _request_model()                 ← LLM call with retry/streaming
    ├─ _execute_tools()                 ← batched, concurrent execution
    ├─ _drain_injections()              ← mid-turn messages & subagent results
    └─ checkpoint / finalize
    │
    ▼
_assemble_outbound()
    │ OutboundMessage(channel, chat_id, content, metadata)
    ▼
MessageBus.outbound
    │
    ▼
[Channel]
```

### 3.4 Mid-Turn Injection

Follow-up messages during active processing are handled without creating competing tasks:

```
User sends follow-up while agent is processing
    │
    ▼
Effective session key matches active task
    │
    ▼
Message goes to session's pending queue (not a new task)
    │
    ▼
AgentRunner._drain_injections() picks it up between iterations
    │
    ▼
Appended to conversation history before next LLM call
```

### 3.5 Subagent Coordination

```
SubagentManager.spawn()
    │ isolated subagent with its own tool registry
    ▼
background task → AgentRunner.run()
    │
    ▼
On completion → InboundMessage(channel="system", sender_id="subagent", ...)
    │ published to bus.inbound with session_key_override
    ▼
Routed to main agent's pending queue (mid-turn injection)
    │
    ▼
Main agent continues with subagent result in context
```

### 3.6 Checkpoint / Recovery

Runtime state is persisted to session metadata at key points:

- **Crash recovery**: `_restore_runtime_checkpoint()` materializes partial context
- **`/stop` handling**: Assistant message + completed tool results + error for pending tool calls
- **Session history preserved**: Conversation can resume from checkpoint

---

## 4. Prompt System

### 4.1 Template Infrastructure

- **Engine**: Jinja2 with `FileSystemLoader` pointing to `nanobot/templates/`
- **Caching**: `@lru_cache` on the Jinja2 environment
- **Templates**: Markdown files with `{% include %}` support and `strip=True`

**Template directory:**

```
nanobot/templates/
  SOUL.md                          (agent personality - bundled default)
  USER.md                          (user profile template - bundled default)
  AGENTS.md                        (workspace instructions for the agent)
  HEARTBEAT.md                     (heartbeat task list template)
  agent/
    identity.md                    (core identity + runtime info + channel hints)
    tool_contract.md               (general tool usage guidelines)
    skills_section.md              (skills summary section)
    platform_policy.md             (Windows vs POSIX policy)
    dream.md                       (Dream memory consolidation instructions)
    evaluator.md                   (notification gate evaluator)
    subagent_system.md             (subagent system prompt)
    subagent_announce.md           (subagent completion announcement)
    cron_reminder.md               (cron job trigger prompt)
    max_iterations_message.md      (iteration limit exceeded message)
    consolidator_archive.md        (pre-consolidation fact extraction)
    _snippets/
      untrusted_content.md         (security warning about external data)
  memory/
    MEMORY.md                      (long-term memory template)
```

### 4.2 System Prompt Assembly

`ContextBuilder.build_system_prompt()` concatenates 8 sections joined by `---` separators:

| # | Section | Source | Always Included? |
|---|---------|--------|-----------------:|
| 1 | **Identity** | `agent/identity.md` — runtime info, workspace path, platform policy, channel format hints, search guidance, untrusted content warning | Yes |
| 2 | **Bootstrap Files** | `AGENTS.md`, `SOUL.md`, `USER.md` from workspace | Only if they exist |
| 3 | **Tool Contract** | `agent/tool_contract.md` — tool usage guidelines | Yes |
| 4 | **Memory Context** | `memory/MEMORY.md` (only if customized) | Conditional |
| 5 | **Active Skills** | Skills marked `always: true` (full SKILL.md body) | Yes |
| 6 | **Skills Summary** | `agent/skills_section.md` — lists all available skills | Yes |
| 7 | **Recent History** | Unprocessed history since last Dream cursor (50 entries / 32k chars) | Yes |
| 8 | **Archived Context Summary** | Session summary if compaction produced one | Conditional |

### 4.3 Message Construction

```python
messages = [
    {"role": "system", "content": <assembled system prompt>},
    ...history...,
    {"role": "user", "content": <user text + runtime context + [media]>},
]
```

Runtime context appended to user messages includes: time, channel, chat_id, sender_id, goal state, MCP status, CLI app attachments.

### 4.4 Key Prompt Templates

**`agent/identity.md`** — Core identity and operational guidance:

```markdown
## Runtime
{platform info}

## Workspace
Your workspace is at: {path}
- Long-term memory: {path}/memory/MEMORY.md (automatically managed by Dream)
- History log: {path}/memory/history.jsonl

{platform_policy}
{channel-specific format hints}

## Search & Discovery
- Prefer built-in grep over exec for workspace search
- {untrusted content warning}

Reply directly with text. Do not use 'message' tool for normal replies.
When you need to call tools before answering, wait for tool results.
Use 'message' only for proactive sends, cross-channel delivery, or file attachments.
```

**`agent/tool_contract.md`** — 66 lines of tool usage guidelines covering: narrowest tool selection, read-before-write, verification, discovery workflows, file operations, process execution, web tools, messaging rules, and scheduling.

**`agent/dream.md`** — Memory consolidation instructions (105 lines):
- Role: "memory consolidation engine" — ruthless about pruning
- File routing table (SOUL.md, USER.md, MEMORY.md, SKILL.md)
- MECE enforcement rules
- History attribute tags: `[skip]`, `[correction]`, `[permanent]`, `[durable]`, `[ephemeral]`
- Delete-or-keep rules with age/decay policies

**`agent/subagent_system.md`** — Subagent identity with runtime context, workspace, and skills summary.

**`agent/evaluator.md`** — Two-part notification gate: system prompt + user task/response to decide whether to notify the user.

**`agent/cron_reminder.md`** — "The scheduled time has arrived. Execute this scheduled cron job now" with rules for direct speech.

**`agent/consolidator_archive.md`** — SNIP criteria (Signal, Novel, Important, Persistent) for pre-consolidation fact extraction.

---

## 5. Memory System

### 5.1 Three-Layer Memory Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Session History (short-term)                  │
│  - history.jsonl (JSONL file, atomic writes + fsync)    │
│  - In-memory cache with invalidation                    │
│  - Legal boundary enforcement (never mid-turn start)    │
│  - TTL-based auto-compaction                            │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Dream Memory (long-term, consolidated)        │
│  - Two-phase consolidation pipeline                     │
│  - Phase 1: SNIP fact extraction                        │
│  - Phase 2: MECE classification & file routing          │
│  - Writes to MEMORY.md, SOUL.md, USER.md, SKILL.md      │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Workspace Files (persistent context)          │
│  - AGENTS.md (workspace-specific instructions)          │
│  - SOUL.md (agent personality)                          │
│  - USER.md (user profile)                               │
│  - HEARTBEAT.md (heartbeat task list)                   │
│  - memory/MEMORY.md (Dream output)                      │
└─────────────────────────────────────────────────────────┘
```

### 5.2 Session Persistence

`SessionManager` (`nanobot/session/manager.py`, ~850 lines):

- **Storage**: JSONL file with atomic writes (temp file + fsync + rename + directory fsync)
- **Caching**: In-memory dict with invalidation
- **History retrieval**: Message count and token budget limits
- **Legal boundaries**: Never start mid-turn, never orphan tool results
- **Session forking**: WebUI replay support
- **File cap**: Enforced with archival callback
- **TTL expiration**: Auto-compaction of idle sessions

### 5.3 Dream Two-Phase Memory Consolidation

Dream runs periodically (configurable interval, default 2 hours) to extract durable knowledge from conversation history.

**Phase 1: SNIP Fact Extraction**

Uses `agent/consolidator_archive.md` to extract facts meeting SNIP criteria:
- **S**ignal: Relevant to the user or workspace
- **N**ovel: Not already in memory
- **I**mportant: Worth remembering
- **P**ersistent: Won't expire soon

Output format: `[mark] fact content` with marks: `permanent`, `durable`, `ephemeral`, `correction`, `skip`

**Phase 2: MECE Classification & File Routing**

Uses `agent/dream.md` to classify and route facts:

| Target File | Content Type |
|-------------|-------------|
| `SOUL.md` | Agent personality, behavioral patterns |
| `USER.md` | User preferences, profile info |
| `MEMORY.md` | Project context, important notes |
| `SKILL.md` | Repeatable procedures (2+ occurrences) |

**MECE enforcement**: Mutually Exclusive, Collectively Exhaustive — no duplicates, proper categorization.

### 5.4 Auto-Compaction

`AutoCompact` (`nanobot/agent/autocompact.py`):

- **TTL-based expiration**: Checks idle sessions against `session_ttl_minutes`
- **Background archival**: Expired sessions archived via `Consolidator`
- **Summary injection**: On session reload — hot path from in-memory dict, cold path from persisted metadata

### 5.5 Consolidator

`Consolidator` (`nanobot/agent/memory.py`, ~1000 lines):

- **Token estimation**: Calculates session prompt size
- **Boundary picking**: Archives at user-turn boundaries (safe for conversation structure)
- **LLM-based summarization**: Compresses archived message chunks
- **Fallback raw archiving**: When LLM summarization fails
- **Replay overflow consolidation**: For messages hidden by message window limits
- **Idle session hard truncation**: Emergency memory pressure relief

---

## 6. Context Management

### 6.1 Token Budget & Context Snipping

The agent operates within token budgets enforced at multiple levels:

| Budget | Location | Default | Effect |
|--------|----------|---------|--------|
| `max_messages` | SessionManager | 120 | Max messages from history |
| `consolidation_ratio` | Consolidator | 0.5 | Target ratio after context compression |
| `max_tool_result_chars` | AgentRunner | 16,000 | Truncates tool output |
| `max_tool_iterations` | AgentRunSpec | 200 | Limits tool call loop |

### 6.2 Context Governance (per iteration)

Before each LLM call, `AgentRunner._prepare_messages()` performs:

1. **Drop orphans**: Remove tool results without matching tool calls
2. **Backfill**: Add missing assistant messages for orphaned tool results
3. **Micro-compaction**: Compress older messages if context is growing
4. **History snipping**: Trim oldest messages to fit token budget

### 6.3 Context Variables

Thread-safe context bound/unbound around tool execution:

- **RequestContext**: Per-request metadata
- **FileStateStore**: File modification tracking
- **WorkspaceScope**: Workspace boundary enforcement

### 6.4 Runtime Context Block

Untrusted metadata appended to user messages at the end of the system prompt:

```
---
[Runtime Context]
Time: {timezone-aware timestamp}
Channel: {telegram|discord|slack|webui|...}
Chat ID: {chat_id}
Sender ID: {sender_id}
Goal State: {active_goal_info}
MCP Status: {connected_servers}
CLI Apps: {attached_files}
```

### 6.5 Bootstrap File Loading

Workspace files are loaded conditionally:

```python
def _load_bootstrap_files(workspace_path):
    files = {}
    for name in ["AGENTS.md", "SOUL.md", "USER.md"]:
        path = workspace_path / name
        if path.exists():
            files[name] = path.read_text()
    return files
```

If a workspace file is identical to the bundled default, it is treated as "not customized" — e.g., `MEMORY.md` is not injected into context if it's still the default.

---

## 7. Skills System

### 7.1 Skill Structure

Skills are markdown directories with YAML frontmatter:

```yaml
---
name: skill-name
description: What the skill does and when to use it
always: true/false          # (optional) always load into context
homepage: URL               # (optional)
metadata:
  nanobot:
    emoji: "icon"
    requires:
      bins: [command1]      # CLI binaries required
      env: [VAR_NAME]       # Environment variables required
---
```

### 7.2 Progressive Disclosure (Three Levels)

| Level | Content | When Loaded |
|-------|---------|-------------|
| 1 | Metadata (name + description) | Always — in skills summary (~100 words) |
| 2 | SKILL.md body | When skill triggers (<5k words) |
| 3 | Bundled resources (scripts/, references/, assets/) | As needed |

### 7.3 Built-in Skills

| Skill | Description | Always? |
|-------|-------------|---------:|
| `memory` | Two-layer memory system (Dream management) | Yes |
| `my` | Self-awareness (model, iterations, context window, token usage) | Yes |
| `cron` | Schedule reminders and recurring tasks | No |
| `github` | Interact with GitHub using `gh` CLI | No |
| `weather` | Get weather via wttr.in and Open-Meteo | No |
| `summarize` | Summarize URLs, files, YouTube videos | No |
| `tmux` | Remote-control tmux sessions | No |
| `clawhub` | Search/install skills from ClawHub registry | No |
| `long-goal` | Sustained objectives via long_task/complete_goal | No |
| `image-generation` | Generate and edit images | No |
| `skill-creator` | Create new skills | No |
| `update-setup` | One-time setup wizard for upgrades | No |

### 7.4 Skill Discovery

- **Locations**: `workspace/skills/` (user-created, priority) and `nanobot/skills/` (built-in)
- **Override**: Workspace skills with the same name override built-in skills
- **Auto-discovery**: `pkgutil` scan + entry-point plugins

---

## 8. Key Configuration

| Field | Default | Effect |
|-------|---------|--------|
| `agents.defaults.disabled_skills` | `[]` | Skills to exclude from loading |
| `agents.defaults.timezone` | `"UTC"` | Time in runtime context |
| `agents.defaults.max_tool_iterations` | `200` | Limits tool call loop |
| `agents.defaults.max_tool_result_chars` | `16,000` | Truncates tool output |
| `agents.defaults.session_ttl_minutes` | `0` | Auto-compaction threshold (0 = disabled) |
| `agents.defaults.max_messages` | `120` | Max messages from history |
| `agents.defaults.consolidation_ratio` | `0.5` | Target ratio after context compression |
| `agents.defaults.dream.enabled` | `true` | Enable Dream memory consolidation |
| `agents.defaults.dream.interval_h` | `2` | Dream runs every N hours |
| `agents.defaults.dream.model_override` | `null` | Override model for Dream sessions |
| `channels.send_progress` | `true` | Stream text progress |
| `channels.show_reasoning` | `true` | Surface model reasoning |
| `gateway.heartbeat.enabled` | `true` | Enable heartbeat cron job |
| `gateway.heartbeat.interval_s` | `1800` | Heartbeat check interval |

Config loaded from `~/.nanobot/config.json` with `${VAR}` environment variable interpolation.

---

## 9. Security Boundaries

| Guard | Location | Rule |
|-------|----------|------|
| **Workspace Restriction** | `tools/filesystem.py` `_resolve_path` | All path resolution must stay under `allowed_dir` + media dir + extra allowed dirs |
| **SSRF Protection** | `security/network.py` `validate_url_target` | Blocks loopback, RFC1918, CGNAT, link-local, cloud metadata. Whitelist via `config.tools.ssrf_whitelist` |
| **Shell Sandbox** | `tools/sandbox.py` | Optional command wrapping (bubblebackend backend). Native shell with workspace restriction on Windows/bare-metal Linux |

---

## 10. Quick Reference: Files of Interest

| File | Purpose |
|------|---------|
| `nanobot/agent/loop.py` | Core agent loop — state machine, session management, context building |
| `nanobot/agent/runner.py` | Agentic iteration loop — LLM calls, tool execution, recovery |
| `nanobot/agent/context.py` | `ContextBuilder` — system prompt assembly, message construction |
| `nanobot/agent/memory.py` | `Consolidator` — Dream memory consolidation, history management |
| `nanobot/agent/autocompact.py` | `AutoCompact` — TTL-based idle session compaction |
| `nanobot/agent/tools/registry.py` | `ToolRegistry` — dynamic tool registration and execution |
| `nanobot/agent/skills.py` | `SkillsLoader` — skill discovery, loading, progressive disclosure |
| `nanobot/session/manager.py` | `SessionManager` — session persistence, history retrieval, forking |
| `nanobot/bus/queue.py` | `MessageBus` — async inbound/outbound message queues |
| `nanobot/config/schema.py` | Pydantic config schema — all configuration fields |
| `nanobot/config/loader.py` | Config loading with `${VAR}` interpolation and migration |
| `nanobot/utils/prompt_templates.py` | Jinja2 template engine with caching |
| `nanobot/templates/` | All prompt templates (Jinja2 markdown) |
| `nanobot/skills/` | Built-in skill definitions |
| `.agent/design.md` | Architecture constraints |
| `.agent/security.md` | Security boundaries |
| `.agent/gotchas.md` | Common gotchas and pitfalls |
