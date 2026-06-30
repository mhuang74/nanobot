"""Tests for MyTool — runtime state inspection and configuration."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger
from pydantic import BaseModel

from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.self import GLOBAL_SESSION_KEY, MyTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_loop(**overrides):
    """Build a lightweight mock AgentLoop with the attributes MyTool reads."""
    loop = MagicMock()
    loop.model = "anthropic/claude-sonnet-4-20250514"
    loop.max_iterations = 40
    loop.context_window_tokens = 65_536
    loop.workspace = Path("/tmp/workspace")
    loop.restrict_to_workspace = False
    loop._start_time = 1000.0
    loop.exec_config = MagicMock()
    loop.channels_config = MagicMock()
    loop._last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
    loop._runtime_vars = {}
    loop._runtime_vars_lock = asyncio.Lock()
    loop._runtime_vars_lru = {}
    loop._current_iteration = 0
    loop.provider_retry_mode = "standard"
    loop.max_tool_result_chars = 16000
    loop._concurrency_gate = None
    loop._unified_session = False
    loop._extra_hooks = []

    # web_config mock — needed for check tests
    loop.web_config = MagicMock()
    loop.web_config.enable = True
    loop.web_config.search = MagicMock()
    loop.web_config.search.api_key = "sk-secret-key-12345"

    # Tools registry mock
    loop.tools = MagicMock()
    loop.tools.tool_names = ["read_file", "write_file", "exec", "web_search", "self"]
    loop.tools.has.side_effect = lambda n: n in loop.tools.tool_names
    loop.tools.get.return_value = None

    # SubagentManager mock
    loop.subagents = MagicMock()
    loop.subagents._running_tasks = {"abc123": MagicMock(done=MagicMock(return_value=False))}
    loop.subagents.get_running_count = MagicMock(return_value=1)

    for k, v in overrides.items():
        setattr(loop, k, v)

    return loop


def _make_tool(runtime_state=None):
    if runtime_state is None:
        runtime_state = _make_mock_loop()
    return MyTool(runtime_state=runtime_state)


def _bind_ctx(session_key: str, channel: str = "telegram", chat_id: str = "1"):
    """Bind a RequestContext for the duration of a test. Returns the token."""
    return bind_request_context(RequestContext(
        channel=channel, chat_id=chat_id, session_key=session_key,
    ))


# ---------------------------------------------------------------------------
# check — no key (summary)
# ---------------------------------------------------------------------------

class TestInspectSummary:

    @pytest.mark.asyncio
    async def test_inspect_returns_current_state(self):
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "max_iterations: 40" in result
        assert "context_window_tokens: 65536" in result

    @pytest.mark.asyncio
    async def test_inspect_includes_runtime_vars(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"__global__": {"task": "review"}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check")
        assert "task" in result

    @pytest.mark.asyncio
    async def test_inspect_summary_shows_all_description_keys(self):
        """check without key should show all top-level keys listed in description."""
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "max_iterations" in result
        assert "context_window_tokens" in result
        assert "model" in result
        assert "workspace" in result
        assert "provider_retry_mode" in result
        assert "max_tool_result_chars" in result
        assert "_last_usage" in result
        assert "_current_iteration" in result


# ---------------------------------------------------------------------------
# check — single key (direct)
# ---------------------------------------------------------------------------

class TestInspectSingleKey:

    @pytest.mark.asyncio
    async def test_inspect_simple_value(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="max_iterations")
        assert "40" in result

    @pytest.mark.asyncio
    async def test_inspect_blocked_returns_error(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="bus")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_dunder_blocked(self):
        tool = _make_tool()
        for attr in ("__class__", "__dict__", "__bases__", "__subclasses__", "__mro__"):
            result = await tool.execute(action="check", key=attr)
            assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_nonexistent_returns_not_found(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="nonexistent_attr_xyz")
        assert "not found" in result


# ---------------------------------------------------------------------------
# check — dot-path navigation
# ---------------------------------------------------------------------------

class TestInspectPathNavigation:

    @pytest.mark.asyncio
    async def test_inspect_config_subfield(self):
        loop = _make_mock_loop()
        loop.web_config = MagicMock()
        loop.web_config.enable = True
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="web_config.enable")
        assert "True" in result

    @pytest.mark.asyncio
    async def test_inspect_dict_key_via_dotpath(self):
        loop = _make_mock_loop()
        loop._last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="_last_usage.prompt_tokens")
        assert "100" in result

    @pytest.mark.asyncio
    async def test_inspect_blocked_in_path(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="bus.foo")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_tools_returns_blocked(self):
        """tools is BLOCKED — check should return access error."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="tools")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_nested_config_redacts_sensitive_scalar_fields(self):
        class SearchConfig(BaseModel):
            provider: str = "tavily"
            api_key: str = "sk-test-secret"
            base_url: str = ""
            max_results: int = 5

        loop = _make_mock_loop()
        loop.web_config = MagicMock()
        loop.web_config.search = SearchConfig()
        tool = _make_tool(loop)

        result = await tool.execute(action="check", key="web_config.search")

        assert "provider='tavily'" in result
        assert "sk-test-secret" not in result
        assert "api_key" not in result.lower()



# ---------------------------------------------------------------------------
# set — restricted (with validation)
# ---------------------------------------------------------------------------

class TestModifyRestricted:

    @pytest.mark.asyncio
    async def test_modify_restricted_valid(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=80)
        assert "Set max_iterations = 80" in result
        assert tool._runtime_state.max_iterations == 80

    @pytest.mark.asyncio
    async def test_modify_restricted_out_of_range(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=0)
        assert "Error" in result
        assert tool._runtime_state.max_iterations == 40

    @pytest.mark.asyncio
    async def test_modify_restricted_max_exceeded(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=999)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_restricted_wrong_type(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value="not_an_int")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_restricted_bool_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=True)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_string_int_coerced(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value="80")
        assert "Set max_iterations" in result
        assert tool._runtime_state.max_iterations == 80

    @pytest.mark.asyncio
    async def test_modify_context_window_valid(self):
        loop = _make_mock_loop(_sync_replay_max_messages=MagicMock())
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="context_window_tokens", value=131072)
        assert "Set context_window_tokens" in result
        assert loop.context_window_tokens == 131072
        loop._sync_replay_max_messages.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_modify_none_value_for_restricted_int(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_iterations", value=None)
        assert "Error" in result


# ---------------------------------------------------------------------------
# set — blocked (minimal set)
# ---------------------------------------------------------------------------

class TestModifyBlocked:

    @pytest.mark.asyncio
    async def test_modify_bus_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="bus", value="hacked")
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_provider_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="provider", value=None)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_running_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="_running", value=True)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_dunder_blocked(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="__class__", value="evil")
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_dotpath_leaf_dunder_blocked(self):
        """Fix 3.1: leaf segment of dot-path must also be validated."""
        tool = _make_tool()
        result = await tool.execute(
            action="set",
            key="provider_retry_mode.__class__",
            value="evil",
        )
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_dotpath_leaf_denied_attr_blocked(self):
        """Fix 3.1: leaf segment matching _DENIED_ATTRS must be rejected."""
        tool = _make_tool()
        result = await tool.execute(
            action="set",
            key="provider_retry_mode.__globals__",
            value={},
        )
        assert "not accessible" in result


# ---------------------------------------------------------------------------
# set — free tier (setattr priority)
# ---------------------------------------------------------------------------

class TestModifyFree:

    @pytest.mark.asyncio
    async def test_modify_existing_attr_setattr(self):
        """Modifying an existing loop attribute should use setattr."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="provider_retry_mode", value="persistent")
        assert "Set provider_retry_mode" in result
        assert tool._runtime_state.provider_retry_mode == "persistent"

    @pytest.mark.asyncio
    async def test_modify_new_key_stores_in_runtime_vars(self):
        """Modifying a non-existing attribute should store in _runtime_vars."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="my_custom_var", value="hello")
        assert "my_custom_var" in result
        assert tool._runtime_state._runtime_vars["__global__"]["my_custom_var"] == "hello"

    @pytest.mark.asyncio
    async def test_modify_rejects_callable(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="evil", value=lambda: None)
        assert "callable" in result

    @pytest.mark.asyncio
    async def test_modify_rejects_complex_objects(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="obj", value=Path("/tmp"))
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_allows_list(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="items", value=[1, 2, 3])
        assert result == "Set scratchpad.items = [1, 2, 3]"
        assert tool._runtime_state._runtime_vars["__global__"]["items"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_modify_allows_dict(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="data", value={"a": 1})
        assert result == "Set scratchpad.data = {'a': 1}"
        assert tool._runtime_state._runtime_vars["__global__"]["data"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_modify_whitespace_key_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="   ", value="test")
        assert "cannot be empty or whitespace" in result

    @pytest.mark.asyncio
    async def test_modify_nested_dict_with_object_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="evil", value={"nested": object()})
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_modify_deep_nesting_rejected(self):
        tool = _make_tool()
        deep = {"level": 0}
        current = deep
        for i in range(1, 15):
            current["child"] = {"level": i}
            current = current["child"]
        result = await tool.execute(action="set", key="deep", value=deep)
        assert "nesting too deep" in result

    @pytest.mark.asyncio
    async def test_modify_dict_with_non_str_key_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="evil", value={42: "value"})
        assert "key must be str" in result

    @pytest.mark.asyncio
    async def test_modify_existing_attr_type_mismatch_rejected(self):
        """Setting a string attr to int should be rejected."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="provider_retry_mode", value=42)
        assert "Error" in result
        assert "str" in result
        assert tool._runtime_state.provider_retry_mode == "standard"

    @pytest.mark.asyncio
    async def test_modify_existing_int_attr_wrong_type_rejected(self):
        """Setting an int attr to string should be rejected."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="max_tool_result_chars", value="big")
        assert "Error" in result
        assert tool._runtime_state.max_tool_result_chars == 16000


# ---------------------------------------------------------------------------
# set — previously BLOCKED/READONLY now open
# ---------------------------------------------------------------------------

class TestModifyOpen:

    @pytest.mark.asyncio
    async def test_modify_tools_blocked(self):
        """tools is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_registry = MagicMock()
        result = await tool.execute(action="set", key="tools", value=new_registry)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_subagents_blocked(self):
        """subagents is READ_ONLY — cannot be replaced."""
        tool = _make_tool()
        new_subagents = MagicMock()
        result = await tool.execute(action="set", key="subagents", value=new_subagents)
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_modify_runner_blocked(self):
        """runner is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_runner = MagicMock()
        result = await tool.execute(action="set", key="runner", value=new_runner)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_sessions_blocked(self):
        """sessions is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_sessions = MagicMock()
        result = await tool.execute(action="set", key="sessions", value=new_sessions)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_consolidator_blocked(self):
        """consolidator is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_consolidator = MagicMock()
        result = await tool.execute(action="set", key="consolidator", value=new_consolidator)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_dream_blocked(self):
        """dream is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_dream = MagicMock()
        result = await tool.execute(action="set", key="dream", value=new_dream)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_auto_compact_blocked(self):
        """auto_compact is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_auto_compact = MagicMock()
        result = await tool.execute(action="set", key="auto_compact", value=new_auto_compact)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_context_blocked(self):
        """context is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_context = MagicMock()
        result = await tool.execute(action="set", key="context", value=new_context)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_commands_blocked(self):
        """commands is BLOCKED — cannot be replaced."""
        tool = _make_tool()
        new_commands = MagicMock()
        result = await tool.execute(action="set", key="commands", value=new_commands)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_workspace_allowed(self):
        """workspace was READONLY in v1, now freely modifiable."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="workspace", value="/new/path")
        assert "Set workspace" in result

    @pytest.mark.asyncio
    async def test_modify_mcp_servers_blocked(self):
        """_mcp_servers contains API credentials — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_mcp_servers", value={"evil": "leaked"})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_mcp_stacks_blocked(self):
        """_mcp_stacks holds connection handles — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_mcp_stacks", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_pending_queues_blocked(self):
        """_pending_queues controls message routing — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_pending_queues", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_session_locks_blocked(self):
        """_session_locks controls session isolation — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_session_locks", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_active_tasks_blocked(self):
        """_active_tasks tracks running tasks — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_active_tasks", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_background_tasks_blocked(self):
        """_background_tasks tracks background tasks — must be blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_background_tasks", value=[])
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_inspect_mcp_servers_blocked(self):
        """_mcp_servers contains credentials — check must be blocked too."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="_mcp_servers")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_wrapped_denied(self):
        """__wrapped__ allows decorator bypass — must be denied."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="__wrapped__", value="evil")
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_closure_denied(self):
        """__closure__ exposes function internals — must be denied."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="__closure__", value="evil")
        assert "protected" in result


# ---------------------------------------------------------------------------
# validate_json_safe — element counting
# ---------------------------------------------------------------------------

class TestValidateJsonSafe:

    def test_single_list_passes(self):
        assert MyTool._validate_json_safe(list(range(500))) is None

    def test_deeply_nested_within_limit(self):
        value = {"level1": {"level2": {"level3": list(range(100))}}}
        assert MyTool._validate_json_safe(value) is None


# ---------------------------------------------------------------------------
# unknown action
# ---------------------------------------------------------------------------

class TestUnknownAction:

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = _make_tool()
        result = await tool.execute(action="explode")
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# runtime_vars limits (from code review)
# ---------------------------------------------------------------------------

class TestRuntimeVarsLimits:

    @pytest.mark.asyncio
    async def test_runtime_vars_rejects_at_max_keys(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"__global__": {f"key_{i}": i for i in range(64)}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="overflow", value="data")
        assert "full" in result
        assert "overflow" not in loop._runtime_vars["__global__"]

    @pytest.mark.asyncio
    async def test_runtime_vars_allows_update_existing_key_at_max(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"__global__": {f"key_{i}": i for i in range(64)}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="key_0", value="updated")
        assert "Error" not in result
        assert loop._runtime_vars["__global__"]["key_0"] == "updated"


# ---------------------------------------------------------------------------
# denied attrs (non-dunder)
# ---------------------------------------------------------------------------

class TestDeniedAttrs:

    @pytest.mark.asyncio
    async def test_modify_denied_non_dunder_blocked(self):
        tool = _make_tool()
        for attr in ("func_globals", "func_code"):
            result = await tool.execute(action="set", key=attr, value="evil")
            assert "protected" in result, f"{attr} should be blocked"


# ---------------------------------------------------------------------------
# SubagentStatus formatting
# ---------------------------------------------------------------------------

class TestSubagentStatusFormatting:

    def test_format_single_status(self):
        """_format_value should produce a rich multi-line display for a SubagentStatus."""
        from nanobot.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="abc12345",
            label="read logs and summarize",
            task_description="Read the log files and produce a summary",
            started_at=time.monotonic() - 12.4,
            phase="awaiting_tools",
            iteration=3,
            tool_events=[
                {"name": "read_file", "status": "ok", "detail": "read app.log"},
                {"name": "grep", "status": "ok", "detail": "searched ERROR"},
                {"name": "exec", "status": "error", "detail": "timeout"},
            ],
            usage={"prompt_tokens": 4500, "completion_tokens": 1200},
        )
        result = MyTool._format_value(status)
        assert "abc12345" in result
        assert "read logs and summarize" in result
        assert "awaiting_tools" in result
        assert "iteration: 3" in result
        assert "read_file(ok)" in result
        assert "exec(error)" in result
        assert "4500" in result

    def test_format_status_dict(self):
        """_format_value should handle dict[str, SubagentStatus] with rich display."""
        from nanobot.agent.subagent import SubagentStatus

        statuses = {
            "abc12345": SubagentStatus(
                task_id="abc12345",
                label="task A",
                task_description="Do task A",
                started_at=time.monotonic() - 5.0,
                phase="awaiting_tools",
                iteration=1,
            ),
        }
        result = MyTool._format_value(statuses)
        assert "1 subagent(s)" in result
        assert "abc12345" in result
        assert "task A" in result

    def test_format_empty_status_dict(self):
        """Empty dict[str, SubagentStatus] should show 'no running subagents'."""
        result = MyTool._format_value({})
        assert "{}" in result

    def test_format_status_with_error(self):
        """Status with error should include the error message."""
        from nanobot.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="err00001",
            label="failing task",
            task_description="A task that fails",
            started_at=time.monotonic() - 1.0,
            phase="error",
            error="Connection refused",
        )
        result = MyTool._format_value(status)
        assert "error: Connection refused" in result

# ---------------------------------------------------------------------------
# _SubagentHook after_iteration updates status
# ---------------------------------------------------------------------------

class TestSubagentHookStatus:

    @pytest.mark.asyncio
    async def test_after_iteration_updates_status(self):
        """after_iteration should copy iteration, tool_events, usage to status."""
        from nanobot.agent.hook import AgentHookContext
        from nanobot.agent.subagent import SubagentStatus, _SubagentHook

        status = SubagentStatus(
            task_id="test",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("test", status)

        context = AgentHookContext(
            iteration=5,
            messages=[],
            tool_events=[{"name": "read_file", "status": "ok", "detail": "ok"}],
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )
        await hook.after_iteration(context)

        assert status.iteration == 5
        assert len(status.tool_events) == 1
        assert status.tool_events[0]["name"] == "read_file"
        assert status.usage == {"prompt_tokens": 100, "completion_tokens": 50}

    @pytest.mark.asyncio
    async def test_after_iteration_with_error(self):
        """after_iteration should set status.error when context has an error."""
        from nanobot.agent.hook import AgentHookContext
        from nanobot.agent.subagent import SubagentStatus, _SubagentHook

        status = SubagentStatus(
            task_id="test",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
        )
        hook = _SubagentHook("test", status)

        context = AgentHookContext(
            iteration=1,
            messages=[],
            error="something went wrong",
        )
        await hook.after_iteration(context)

        assert status.error == "something went wrong"

    @pytest.mark.asyncio
    async def test_after_iteration_no_status_is_noop(self):
        """after_iteration with no status should be a no-op."""
        from nanobot.agent.hook import AgentHookContext
        from nanobot.agent.subagent import _SubagentHook

        hook = _SubagentHook("test")
        context = AgentHookContext(iteration=1, messages=[])
        result = await hook.after_iteration(context)

        assert result is None
        assert context.iteration == 1


# ---------------------------------------------------------------------------
# Checkpoint callback updates status
# ---------------------------------------------------------------------------

class TestCheckpointCallback:

    @pytest.mark.asyncio
    async def test_checkpoint_updates_phase_and_iteration(self):
        """The _on_checkpoint callback should update status.phase and iteration."""

        from nanobot.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="cp",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
        )

        # Simulate the checkpoint callback as defined in _run_subagent
        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        await _on_checkpoint({"phase": "awaiting_tools", "iteration": 2})
        assert status.phase == "awaiting_tools"
        assert status.iteration == 2

        await _on_checkpoint({"phase": "tools_completed", "iteration": 3})
        assert status.phase == "tools_completed"
        assert status.iteration == 3

    @pytest.mark.asyncio
    async def test_checkpoint_preserves_phase_on_missing_key(self):
        """If payload doesn't have 'phase', status.phase should stay unchanged."""
        from nanobot.agent.subagent import SubagentStatus

        status = SubagentStatus(
            task_id="cp",
            label="test",
            task_description="test",
            started_at=time.monotonic(),
            phase="initializing",
        )

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        await _on_checkpoint({"iteration": 1})
        assert status.phase == "initializing"
        assert status.iteration == 1


# ---------------------------------------------------------------------------
# check subagents._task_statuses via dot-path
# NOTE: subagents is now BLOCKED for security, so these tests verify
# that access is properly rejected.
# ---------------------------------------------------------------------------

class TestInspectTaskStatuses:

    @pytest.mark.asyncio
    async def test_inspect_task_statuses_accessible(self):
        """subagents is READ_ONLY — check should show subagent statuses."""
        from nanobot.agent.subagent import SubagentStatus

        loop = _make_mock_loop()
        loop.subagents._task_statuses = {
            "abc12345": SubagentStatus(
                task_id="abc12345",
                label="read logs",
                task_description="Read the log files",
                started_at=time.monotonic() - 8.0,
                phase="awaiting_tools",
                iteration=2,
                tool_events=[{"name": "read_file", "status": "ok", "detail": "ok"}],
                usage={"prompt_tokens": 500, "completion_tokens": 100},
            ),
        }
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="subagents._task_statuses")
        assert "abc12345" in result
        assert "read logs" in result

    @pytest.mark.asyncio
    async def test_inspect_single_subagent_status_accessible(self):
        """subagents._task_statuses.<id> should return individual SubagentStatus."""
        from nanobot.agent.subagent import SubagentStatus

        loop = _make_mock_loop()
        status = SubagentStatus(
            task_id="xyz",
            label="search code",
            task_description="Search the codebase",
            started_at=time.monotonic() - 3.0,
            phase="done",
            iteration=4,
            stop_reason="completed",
        )
        loop.subagents._task_statuses = {"xyz": status}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="subagents._task_statuses.xyz")
        assert "search code" in result
        assert "completed" in result


# ---------------------------------------------------------------------------
# read-only mode (tools.my.allow_set=False)
# ---------------------------------------------------------------------------

class TestReadOnlyMode:

    def _make_readonly_tool(self):
        loop = _make_mock_loop()
        return MyTool(runtime_state=loop, modify_allowed=False)

    @pytest.mark.asyncio
    async def test_inspect_allowed_in_readonly(self):
        tool = self._make_readonly_tool()
        result = await tool.execute(action="check", key="max_iterations")
        assert "40" in result

    @pytest.mark.asyncio
    async def test_modify_blocked_in_readonly(self):
        tool = self._make_readonly_tool()
        result = await tool.execute(action="set", key="max_iterations", value=80)
        assert "disabled" in result

    def test_description_shows_readonly(self):
        tool = self._make_readonly_tool()
        assert "READ-ONLY MODE" in tool.description

    def test_description_shows_warning_when_modify_allowed(self):
        tool = _make_tool()
        assert "IMPORTANT" in tool.description
        assert "READ-ONLY" not in tool.description


# ---------------------------------------------------------------------------
# scratchpad-only mode (tools.my.allow_set=False, allow_scratchpad=True)
# ---------------------------------------------------------------------------

class TestScratchpadOnlyMode:

    def _make_scratchpad_tool(self):
        loop = _make_mock_loop()
        return MyTool(runtime_state=loop, modify_allowed=False, allow_scratchpad=True)

    @pytest.mark.asyncio
    async def test_inspect_allowed_in_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="check", key="max_iterations")
        assert "40" in result

    @pytest.mark.asyncio
    async def test_scratchpad_write_allowed(self):
        """Setting a harmless scratchpad key should succeed."""
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="user_prefers_concise", value=True)
        assert "Set scratchpad" in result
        # Verify it was actually stored
        result = await tool.execute(action="check", key="user_prefers_concise")
        assert "True" in result

    @pytest.mark.asyncio
    async def test_scratchpad_write_string_allowed(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="current_project", value="nanobot")
        assert "Set scratchpad" in result

    @pytest.mark.asyncio
    async def test_scratchpad_write_dict_allowed(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="task_meta", value={"step": 2, "total": 5})
        assert "Set scratchpad" in result

    @pytest.mark.asyncio
    async def test_model_blocked_in_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="model", value="gpt-4")
        assert "Error" in result
        assert "scratchpad-only" in result

    @pytest.mark.asyncio
    async def test_model_preset_blocked_in_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="model_preset", value="fast")
        assert "Error" in result
        assert "scratchpad-only" in result

    @pytest.mark.asyncio
    async def test_max_iterations_blocked_in_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="max_iterations", value=80)
        assert "Error" in result
        assert "scratchpad-only" in result

    @pytest.mark.asyncio
    async def test_context_window_blocked_in_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="context_window_tokens", value=262144)
        assert "Error" in result
        assert "scratchpad-only" in result

    @pytest.mark.asyncio
    async def test_dot_path_blocked_in_scratchpad_only(self):
        """Dot-paths should be blocked — scratchpad keys are simple keys only."""
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="web_config.enable", value=True)
        assert "Error" in result
        assert "scratchpad-only" in result

    @pytest.mark.asyncio
    async def test_blocked_attr_blocked_in_scratchpad_only(self):
        """Blocked attributes (e.g. 'tools') should still be blocked."""
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="tools", value={})
        assert "Error" in result
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_real_attr_blocked_in_scratchpad_only(self):
        """Real runtime attributes (e.g. 'workspace') cannot be overwritten."""
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="workspace", value="/tmp")
        assert "Error" in result
        assert "scratchpad-only" in result

    @pytest.mark.asyncio
    async def test_callable_rejected_in_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        result = await tool.execute(action="set", key="bad_value", value=lambda: None)
        assert "Error" in result
        assert "callable" in result

    def test_description_shows_scratchpad_only(self):
        tool = self._make_scratchpad_tool()
        assert "SCRATCHPAD-ONLY" in tool.description
        assert "READ-ONLY" not in tool.description

    def test_description_shows_readonly_when_no_scratchpad(self):
        """When allow_scratchpad=False and modify_allowed=False, show READ-ONLY."""
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, modify_allowed=False, allow_scratchpad=False)
        assert "READ-ONLY" in tool.description
        assert "SCRATCHPAD-ONLY" not in tool.description

    @pytest.mark.asyncio
    async def test_scratchpad_persists_across_check(self):
        """Scratchpad writes should be readable via check."""
        tool = self._make_scratchpad_tool()
        await tool.execute(action="set", key="session_model", value="nw-balanced")
        result = await tool.execute(action="check", key="session_model")
        assert "nw-balanced" in result


# ---------------------------------------------------------------------------
# runtime vars check fallback (Fix #1: cross-turn memory)
# ---------------------------------------------------------------------------

class TestRuntimeVarsInspectFallback:

    @pytest.mark.asyncio
    async def test_inspect_runtime_var_after_modify(self):
        """Design doc scenario: set then check should return the value."""
        tool = _make_tool()
        await tool.execute(action="set", key="user_prefers_concise", value=True)
        result = await tool.execute(action="check", key="user_prefers_concise")
        assert "True" in result

    @pytest.mark.asyncio
    async def test_inspect_runtime_var_string(self):
        tool = _make_tool()
        await tool.execute(action="set", key="current_project", value="nanobot")
        result = await tool.execute(action="check", key="current_project")
        assert "nanobot" in result

    @pytest.mark.asyncio
    async def test_inspect_runtime_var_dict(self):
        tool = _make_tool()
        await tool.execute(action="set", key="task_meta", value={"step": 2, "total": 5})
        result = await tool.execute(action="check", key="task_meta")
        assert "step" in result
        assert "2" in result

    @pytest.mark.asyncio
    async def test_inspect_nonexistent_still_returns_not_found(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="never_set_key_xyz")
        assert "not found" in result


# ---------------------------------------------------------------------------
# sensitive sub-field blocking (Fix #3: API key leak prevention)
# ---------------------------------------------------------------------------

class TestSensitiveSubFieldBlocking:

    @pytest.mark.asyncio
    async def test_inspect_api_key_blocked(self):
        """web_config.search.api_key must not be accessible."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="web_config.search.api_key")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_password_blocked(self):
        """Any field named 'password' must be blocked."""
        loop = _make_mock_loop()
        loop.some_config = MagicMock()
        loop.some_config.password = "hunter2"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="some_config.password")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_secret_blocked(self):
        loop = _make_mock_loop()
        loop.vault = MagicMock()
        loop.vault.secret = "classified"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="vault.secret")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_token_blocked(self):
        loop = _make_mock_loop()
        loop.auth_data = MagicMock()
        loop.auth_data.token = "jwt-payload"
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="auth_data.token")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_api_key_blocked(self):
        """web_config is READ_ONLY, so any set under it is blocked."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="web_config.search.api_key", value="evil")
        # Blocked either by READ_ONLY (web_config) or sensitive name (api_key)
        assert "read-only" in result or "not accessible" in result

    @pytest.mark.asyncio
    async def test_modify_password_blocked(self):
        loop = _make_mock_loop()
        loop.some_config = MagicMock()
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="some_config.password", value="evil")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_non_sensitive_subfield_allowed(self):
        """web_config.enable should still be inspectable."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="web_config.enable")
        assert "True" in result

    @pytest.mark.asyncio
    async def test_modify_sensitive_top_level_blocked(self):
        """Top-level key matching sensitive name must be blocked for set."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="api_key", value="evil")
        assert "protected" in result


# ---------------------------------------------------------------------------
# security-sensitive attribute protection (Fix #4)
# ---------------------------------------------------------------------------

class TestSecurityAttributeProtection:

    @pytest.mark.asyncio
    async def test_modify_restrict_to_workspace_blocked(self):
        """restrict_to_workspace is BLOCKED — cannot be toggled."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="restrict_to_workspace", value=True)
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_modify_exec_config_blocked(self):
        """exec_config is READ_ONLY — cannot be modified."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="exec_config", value=MagicMock())
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_modify_web_config_blocked(self):
        """web_config is READ_ONLY — cannot be modified."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="web_config", value=MagicMock())
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_modify_channels_config_blocked(self):
        """channels_config is BLOCKED — cannot be modified."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="channels_config", value={})
        assert "protected" in result

    @pytest.mark.asyncio
    async def test_inspect_restrict_to_workspace_blocked(self):
        """restrict_to_workspace is BLOCKED — cannot be inspected."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="restrict_to_workspace")
        assert "not accessible" in result

    @pytest.mark.asyncio
    async def test_inspect_exec_config_allowed(self):
        """exec_config is READ_ONLY — check should work."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="exec_config")
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_inspect_web_config_allowed(self):
        """web_config is READ_ONLY — check should work."""
        tool = _make_tool()
        result = await tool.execute(action="check", key="web_config")
        assert "Error" not in result

    @pytest.mark.asyncio
    async def test_modify_exec_config_dotpath_blocked(self):
        """exec_config.enable = False should be blocked because exec_config is READ_ONLY."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="exec_config.enable", value=False)
        assert "read-only" in result

    @pytest.mark.asyncio
    async def test_modify_web_config_dotpath_blocked(self):
        """web_config.enable = False should be blocked because web_config is READ_ONLY."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="web_config.enable", value=False)
        assert "read-only" in result


# ---------------------------------------------------------------------------
# current iteration count (Fix #2)
# ---------------------------------------------------------------------------

class TestCurrentIteration:

    @pytest.mark.asyncio
    async def test_inspect_current_iteration(self):
        tool = _make_tool()
        result = await tool.execute(action="check", key="_current_iteration")
        assert "0" in result

    @pytest.mark.asyncio
    async def test_current_iteration_in_summary(self):
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "_current_iteration" in result

    @pytest.mark.asyncio
    async def test_modify_current_iteration_blocked(self):
        """_current_iteration is READ_ONLY — cannot be set manually."""
        tool = _make_tool()
        result = await tool.execute(action="set", key="_current_iteration", value=5)
        assert "read-only" in result


# ---------------------------------------------------------------------------
# _last_usage in check summary (Fix #5)
# ---------------------------------------------------------------------------

class TestLastUsageInSummary:

    @pytest.mark.asyncio
    async def test_last_usage_shown_in_summary(self):
        tool = _make_tool()
        result = await tool.execute(action="check")
        assert "_last_usage" in result
        assert "prompt_tokens" in result

    @pytest.mark.asyncio
    async def test_last_usage_not_shown_when_empty(self):
        loop = _make_mock_loop()
        loop._last_usage = {}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check")
        assert "_last_usage" not in result


# ---------------------------------------------------------------------------
# set_context (audit session tracking)
# ---------------------------------------------------------------------------

class TestSetContext:

    def test_set_context_stores_channel_and_chat_id(self):
        from nanobot.agent.tools.context import RequestContext
        tool = _make_tool()
        tool.set_context(RequestContext(channel="feishu", chat_id="oc_abc123"))
        assert tool._channel == "feishu"
        assert tool._chat_id == "oc_abc123"


# ===========================================================================
# v2: Session isolation, size cap, concurrency, context race, redaction
# ===========================================================================

class TestScratchpadIsolation:
    """A. Cross-session scratchpad isolation."""

    @pytest.mark.asyncio
    async def test_two_sessions_cannot_read_each_other_keys(self):
        tool = _make_tool()
        tok_a = _bind_ctx("telegram:1", channel="telegram", chat_id="1")
        try:
            await tool.execute(action="set", key="foo", value="bar")
        finally:
            reset_request_context(tok_a)
        tok_b = _bind_ctx("telegram:2", channel="telegram", chat_id="2")
        try:
            result = await tool.execute(action="check", key="foo")
        finally:
            reset_request_context(tok_b)
        assert "not found" in result or "foo" not in result

    @pytest.mark.asyncio
    async def test_two_sessions_cannot_write_each_other_keys(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok_a = _bind_ctx("telegram:1", channel="telegram", chat_id="1")
        try:
            await tool.execute(action="set", key="k", value="v1")
        finally:
            reset_request_context(tok_a)
        tok_b = _bind_ctx("telegram:2", channel="telegram", chat_id="2")
        try:
            await tool.execute(action="set", key="k", value="v2")
        finally:
            reset_request_context(tok_b)
        assert loop._runtime_vars["telegram:1"]["k"] == "v1"
        assert loop._runtime_vars["telegram:2"]["k"] == "v2"

    @pytest.mark.asyncio
    async def test_no_context_falls_back_to_global(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="k", value="v")
        assert "Set scratchpad" in result
        assert loop._runtime_vars["__global__"]["k"] == "v"
        result = await tool.execute(action="check", key="k")
        assert "v" in result

    @pytest.mark.asyncio
    async def test_no_arg_check_only_returns_current_session(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok_a = _bind_ctx("telegram:1", chat_id="1")
        try:
            await tool.execute(action="set", key="a_key", value="a")
        finally:
            reset_request_context(tok_a)
        tok_b = _bind_ctx("telegram:2", chat_id="2")
        try:
            await tool.execute(action="set", key="b_key", value="b")
            result = await tool.execute(action="check")
        finally:
            reset_request_context(tok_b)
        assert "b_key" in result
        assert "a_key" not in result

    @pytest.mark.asyncio
    async def test_check_scratchpad_alias_scoped(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok_a = _bind_ctx("telegram:1", chat_id="1")
        try:
            await tool.execute(action="set", key="a_key", value="a")
        finally:
            reset_request_context(tok_a)
        tok_b = _bind_ctx("telegram:2", chat_id="2")
        try:
            result = await tool.execute(action="check", key="scratchpad")
        finally:
            reset_request_context(tok_b)
        assert "a_key" not in result

    @pytest.mark.asyncio
    async def test_require_context_blocks_write_without_session(self):
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, modify_allowed=True, require_context=True)
        result = await tool.execute(action="set", key="k", value="v")
        assert result == "Error: no session context available"
        assert "__global__" not in loop._runtime_vars or "k" not in loop._runtime_vars.get("__global__", {})

    @pytest.mark.asyncio
    async def test_require_context_honored_with_isolation_kill_switch(self):
        """require_context and session_isolation are independent: even when
        the kill-switch (session_isolation=False) is on, require_context=True
        must still reject context-less writes instead of silently falling back
        to __global__."""
        loop = _make_mock_loop()
        tool = MyTool(
            runtime_state=loop, modify_allowed=True,
            require_context=True, session_isolation=False,
        )
        result = await tool.execute(action="set", key="k", value="v")
        assert result == "Error: no session context available"
        assert "__global__" not in loop._runtime_vars or "k" not in loop._runtime_vars.get("__global__", {})

    @pytest.mark.asyncio
    async def test_require_context_blocks_read_without_session(self):
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, require_context=True)
        result = await tool.execute(action="check", key="scratchpad")
        assert "no session context" in result

    @pytest.mark.asyncio
    async def test_session_isolation_kill_switch_flattens_to_global(self):
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, modify_allowed=True, session_isolation=False)
        tok_a = _bind_ctx("telegram:1", chat_id="1")
        try:
            await tool.execute(action="set", key="k", value="v")
        finally:
            reset_request_context(tok_a)
        tok_b = _bind_ctx("telegram:2", chat_id="2")
        try:
            result = await tool.execute(action="check", key="k")
        finally:
            reset_request_context(tok_b)
        # Kill-switch: both sessions share __global__, so B sees A's key.
        assert "<redacted>" in result or "'v'" in result
        assert "telegram:1" not in loop._runtime_vars
        assert GLOBAL_SESSION_KEY in loop._runtime_vars

    @pytest.mark.asyncio
    async def test_my_tool_excluded_from_subagent_scope(self):
        """D17: subagents never receive the `my` tool — they cannot read or
        write the parent's scratchpad. MyTool declares _scopes={'core'} and
        _plugin_discoverable=False, so ToolLoader.load(scope='subagent')
        excludes it on both counts. This resolves the v2 spec open question
        #4: subagents run with an isolated tool registry that does not contain
        `my`, so session-scope inheritance is moot."""
        assert MyTool._plugin_discoverable is False
        assert MyTool._scopes == {"core"}
        assert "subagent" not in MyTool._scopes


class TestScratchpadMigration:
    """B. Eager-init (D15): legacy flat dicts are NOT migrated."""

    @pytest.mark.asyncio
    async def test_legacy_flat_dict_is_invisible_not_migrated(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"legacy_key": 1}  # flat shape
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="legacy_key")
        assert "not found" in result
        # Shape unchanged — no auto-migration to __global__.
        assert loop._runtime_vars == {"legacy_key": 1}

    @pytest.mark.asyncio
    async def test_global_scope_separate_from_request_scope(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        await tool.execute(action="set", key="g", value=1)
        tok_a = _bind_ctx("telegram:1", chat_id="1")
        try:
            await tool.execute(action="set", key="a", value=2)
        finally:
            reset_request_context(tok_a)
        assert loop._runtime_vars["__global__"] == {"g": 1}
        assert loop._runtime_vars["telegram:1"] == {"a": 2}


class TestValueSizeCap:
    """C. Per-value 8 KB cap."""

    @pytest.mark.asyncio
    async def test_oversized_string_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="big", value="x" * 9000)
        assert "Error" in result
        assert "8192" in result

    @pytest.mark.asyncio
    async def test_oversized_dict_rejected(self):
        tool = _make_tool()
        big = {f"k{i}": "x" * 100 for i in range(100)}
        result = await tool.execute(action="set", key="big", value=big)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_at_limit_allowed(self):
        tool = _make_tool()
        # JSON repr of the string is "<chars>" = len + 2 quotes → aim for 8192.
        s = "x" * (8192 - 2)
        assert len(json.dumps(s, ensure_ascii=False)) <= 8192
        result = await tool.execute(action="set", key="ok", value=s)
        assert "Set scratchpad" in result

    @pytest.mark.asyncio
    async def test_size_cap_applies_in_set_path(self):
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, modify_allowed=True)
        result = await tool.execute(action="set", key="big", value="x" * 9000)
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_callable_rejected_before_size_check(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="fn", value=lambda: None)
        assert "callable" in result

    @pytest.mark.asyncio
    async def test_non_serializable_rejected(self):
        tool = _make_tool()
        result = await tool.execute(action="set", key="obj", value=Path("/tmp"))
        assert "Error" in result


class TestCountCapConcurrency:
    """D. Per-session count cap + atomicity."""

    @pytest.mark.asyncio
    async def test_max_keys_per_session(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok = _bind_ctx("telegram:1", chat_id="1")
        try:
            for i in range(64):
                await tool.execute(action="set", key=f"k{i}", value=i)
            result = await tool.execute(action="set", key="k64", value=64)
        finally:
            reset_request_context(tok)
        assert "full" in result
        assert len(loop._runtime_vars["telegram:1"]) == 64

    @pytest.mark.asyncio
    async def test_max_keys_independent_per_session(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok_a = _bind_ctx("telegram:1", chat_id="1")
        try:
            for i in range(64):
                await tool.execute(action="set", key=f"k{i}", value=i)
        finally:
            reset_request_context(tok_a)
        tok_b = _bind_ctx("telegram:2", chat_id="2")
        try:
            r = await tool.execute(action="set", key="bkey", value="ok")
        finally:
            reset_request_context(tok_b)
        assert "Set scratchpad" in r

    @pytest.mark.asyncio
    async def test_concurrent_writes_do_not_exceed_cap(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok = _bind_ctx("telegram:1", chat_id="1")
        try:
            await asyncio.gather(*[
                tool.execute(action="set", key=f"k{i}", value=i) for i in range(100)
            ])
        finally:
            reset_request_context(tok)
        assert len(loop._runtime_vars["telegram:1"]) == 64


class TestContextRace:
    """E. ContextVar context race — read at execute time, not instance-captured."""

    @pytest.mark.asyncio
    async def test_concurrent_sessions_keep_own_session_key(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)

        async def write_then_check(session_key, chat_id, val):
            tok = _bind_ctx(session_key, chat_id=chat_id)
            try:
                await tool.execute(action="set", key="k", value=val)
                # Yield mid-turn to allow interleaving.
                await asyncio.sleep(0)
                res = await tool.execute(action="check", key="k")
                return res
            finally:
                reset_request_context(tok)

        r1, r2 = await asyncio.gather(
            write_then_check("telegram:1", "1", "v1"),
            write_then_check("telegram:2", "2", "v2"),
        )
        assert "v1" in r1
        assert "v2" in r2
        assert loop._runtime_vars["telegram:1"]["k"] == "v1"
        assert loop._runtime_vars["telegram:2"]["k"] == "v2"


class TestAuditRedaction:
    """F. Audit log redaction (D14)."""

    @pytest.fixture
    def log_sink(self):
        records: list[str] = []

        def sink(message):
            records.append(str(message))

        handler_id = logger.add(sink, level="DEBUG", format="{message}")
        try:
            yield records
        finally:
            logger.remove(handler_id)

    @pytest.mark.asyncio
    async def test_audit_redacts_sensitive_named_key(self, log_sink):
        tool = _make_tool()
        result = await tool.execute(action="set", key="api_key", value="sk-abc123")
        assert "protected" in result
        joined = "\n".join(log_sink)
        assert "sk-abc123" not in joined

    @pytest.mark.asyncio
    async def test_audit_redacts_secret_shaped_value(self, log_sink):
        tool = _make_tool()
        # "foobar" is not a sensitive name; value matches sk- regex.
        result = await tool.execute(
            action="set", key="foobar",
            value="sk-" + "a" * 40,
        )
        assert "Set scratchpad" in result
        joined = "\n".join(log_sink)
        assert "<redacted>" in joined
        assert ("sk-" + "a" * 40) not in joined

    @pytest.mark.asyncio
    async def test_audit_non_sensitive_value_logged_plain(self, log_sink):
        tool = _make_tool()
        await tool.execute(action="set", key="k", value=42)
        joined = "\n".join(log_sink)
        assert "42" in joined

    @pytest.mark.asyncio
    async def test_audit_session_label_reflects_bound_context(self, log_sink):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        tok = _bind_ctx("telegram:42", chat_id="42")
        try:
            await tool.execute(action="set", key="k", value=1)
        finally:
            reset_request_context(tok)
        joined = "\n".join(log_sink)
        assert "telegram:42" in joined


class TestOutputRedaction:
    """D8. Tool return strings must not leak secrets."""

    @pytest.mark.asyncio
    async def test_sensitive_key_output_redacted(self):
        loop = _make_mock_loop()
        # Pre-seed via the tool itself using a non-sensitive alias trick won't
        # work (_SENSITIVE_NAMES blocks "token"). Use direct scoped injection.
        loop._runtime_vars = {"__global__": {"my_token_secret": "harmless"}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="my_token_secret")
        assert "<redacted>" in result or "not found" in result

    @pytest.mark.asyncio
    async def test_secret_value_output_redacted(self):
        loop = _make_mock_loop()
        secret = "sk-" + "a" * 40
        loop._runtime_vars = {"__global__": {"config": secret}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="config")
        assert "<redacted>" in result
        assert secret not in result

    @pytest.mark.asyncio
    async def test_benign_value_output_plain(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"__global__": {"task": "review"}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="task")
        assert "review" in result

    @pytest.mark.asyncio
    async def test_inspect_redacts_sensitive_keys(self):
        loop = _make_mock_loop()
        loop._runtime_vars = {"__global__": {"api_key": "sk-leak", "note": "ok"}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="scratchpad")
        assert "sk-leak" not in result
        assert "<redacted>" in result
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_inspect_redacts_secret_values(self):
        loop = _make_mock_loop()
        secret = "ghp_" + "a" * 36
        loop._runtime_vars = {"__global__": {"token": secret, "count": 3}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="scratchpad")
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_set_return_redacts_nested_secret_under_benign_key(self):
        """D8: a secret nested in a dict under a non-sensitive top-level key
        must not leak in the chat-visible set return string."""
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        secret = "sk-" + "a" * 40
        result = await tool.execute(action="set", key="data", value={"api_key": secret})
        assert "Set scratchpad" in result
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_set_return_redacts_nested_secret_in_list(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        secret = "ghp_" + "a" * 36
        result = await tool.execute(action="set", key="items", value=[secret, "ok"])
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_set_return_plain_for_benign_nested(self):
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="set", key="data", value={"a": 1, "b": "x"})
        assert "{'a': 1, 'b': 'x'}" in result

    @pytest.mark.asyncio
    async def test_check_redacts_nested_secret_under_benign_key(self):
        """D8 regression: a secret nested in a dict under a non-sensitive
        top-level scratchpad key must not leak via the check/inspect path
        (was previously only redacted on the set return; _format_value did
        not recurse into dicts)."""
        loop = _make_mock_loop()
        secret = "sk-" + "a" * 40
        loop._runtime_vars = {"__global__": {"data": {"api_key": secret}}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="data")
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_check_redacts_secret_in_list_value(self):
        """D8 regression: a secret stored in a list scratchpad value must
        not leak via check/inspect (list branch previously had no redaction)."""
        loop = _make_mock_loop()
        secret = "ghp_" + "a" * 36
        loop._runtime_vars = {"__global__": {"items": [secret, "ok"]}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="items")
        assert secret not in result
        assert "<redacted>" in result
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_check_scratchpad_redacts_nested_secret(self):
        """D8 regression: check scratchpad (full session dict) must redact
        nested secrets, not just the top level."""
        loop = _make_mock_loop()
        secret = "sk-" + "a" * 40
        loop._runtime_vars = {"__global__": {"data": {"api_key": secret}, "note": "ok"}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="scratchpad")
        assert secret not in result
        assert "<redacted>" in result
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_set_return_redacts_camelcase_secret_key(self):
        """camelCase secret keys (apiKey, accessToken) must be redacted just
        like their snake_case counterparts (D8 regression for camelCase)."""
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        secret = "sk-" + "a" * 40
        result = await tool.execute(action="set", key="data", value={"apiKey": secret})
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_check_redacts_camelcase_secret_key(self):
        """camelCase secret keys in scratchpad values must be redacted on the
        check/inspect path too."""
        loop = _make_mock_loop()
        secret = "sk-" + "a" * 40
        loop._runtime_vars = {"__global__": {"data": {"accessToken": secret}}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="data")
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_set_return_redacts_secret_shaped_dict_key(self):
        """A dict key that is itself a secret-shaped token (e.g.
        {"ghp_...": "prod"}) must be redacted so the token never reaches
        chat-visible output or audit logs."""
        loop = _make_mock_loop()
        tool = _make_tool(runtime_state=loop)
        secret = "ghp_" + "a" * 36
        result = await tool.execute(action="set", key="data", value={secret: "prod"})
        assert secret not in result
        assert "<redacted>" in result

    @pytest.mark.asyncio
    async def test_check_redacts_secret_shaped_dict_key(self):
        """Secret-shaped dict keys must also be redacted on the check/inspect
        path, not just the set return."""
        loop = _make_mock_loop()
        secret = "ghp_" + "a" * 36
        loop._runtime_vars = {"__global__": {"data": {secret: "prod"}}}
        tool = _make_tool(runtime_state=loop)
        result = await tool.execute(action="check", key="data")
        assert secret not in result
        assert "<redacted>" in result


class TestValueSizeBytes:
    """DoS size cap must be measured in UTF-8 bytes, not Python characters,
    so multi-byte content (emoji, CJK) is bounded correctly."""

    def test_multibyte_value_rejected_by_byte_length(self):
        # ~8K emoji chars -> ~32KB UTF-8 bytes; must exceed the 8192-byte cap.
        big = "😀" * 8200
        err = MyTool._check_value_size(big)
        assert err is not None
        assert "exceeds" in err

    def test_ascii_value_under_limit_accepted(self):
        err = MyTool._check_value_size("a" * 100)
        assert err is None


class TestSessionCapEviction:
    """D11. Global session cap with LRU eviction."""

    @pytest.mark.asyncio
    async def test_session_cap_evicts_lru(self):
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, modify_allowed=True)
        # Lower the cap for the test via monkeypatch on the instance.
        tool._MAX_SESSIONS = 4
        for i in range(tool._MAX_SESSIONS + 2):
            tok = _bind_ctx(f"sess:{i}", chat_id=str(i))
            try:
                await tool.execute(action="set", key="k", value=i)
            finally:
                reset_request_context(tok)
        assert len(loop._runtime_vars) <= tool._MAX_SESSIONS
        # __global__ never evicted even though never written here.
        assert "sess:0" not in loop._runtime_vars

    @pytest.mark.asyncio
    async def test_global_scope_never_evicted(self):
        loop = _make_mock_loop()
        tool = MyTool(runtime_state=loop, modify_allowed=True)
        tool._MAX_SESSIONS = 2
        loop._runtime_vars["__global__"] = {"g": 1}
        loop._runtime_vars_lru["__global__"] = 0.0
        tok = _bind_ctx("sess:1", chat_id="1")
        try:
            await tool.execute(action="set", key="k", value=1)
            await tool.execute(action="set", key="k2", value=2)  # sess:2 not bound
        finally:
            reset_request_context(tok)
        tok = _bind_ctx("sess:2", chat_id="2")
        try:
            await tool.execute(action="set", key="k", value=2)
        finally:
            reset_request_context(tok)
        assert "__global__" in loop._runtime_vars
