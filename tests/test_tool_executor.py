import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent.agent.loop import Agent, AgentConversation
from harness_code_agent.runtime import tools
from harness_code_agent.runtime.middlewares import AgentMiddleware
from harness_code_agent.runtime.permissions import PermissionPolicy
from harness_code_agent.runtime.tool_context import ToolContext
from harness_code_agent.runtime.tool_result import ToolResult
from harness_code_agent.sessions.events import EventBus
from harness_code_agent.workspace.service import WorkspaceService


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tool_call(call_id: str, name: str, args: dict | str | None = None):
    arguments = args if isinstance(args, str) else json.dumps(args or {})
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _FakeCompletions:
    def __init__(self, tool_calls):
        self.calls = 0
        self._tool_calls = tool_calls

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=None, tool_calls=self._tool_calls),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )


def _conversation_with_registry(root: Path, registry: tools.ToolRegistry, tool_calls, middlewares=None):
    context = ToolContext(
        workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
        permission_policy=PermissionPolicy(mode="danger-full-access"),
        event_bus=EventBus(),
        tool_registry=registry,
    )
    schemas = registry.schemas()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(tool_calls)))
    with patch("harness_code_agent.agent.conversation.get_client", return_value=fake_client):
        conversation = AgentConversation(
            Agent(
                "main_agent",
                "system",
                use_tools=True,
                tool_schemas=schemas,
                middlewares=list(middlewares or []),
                tool_context=context,
            )
        )
    return conversation, context


class ToolExecutorTests(unittest.TestCase):
    def test_executor_pool_is_instance_scoped(self):
        from harness_code_agent.agent.tool_executor import ToolExecutor

        self.assertNotIn("_executor", ToolExecutor.__dict__)

    def test_shell_lane_classification_is_conservative(self):
        from harness_code_agent.agent.tool_executor import classify_shell_lane

        cases = {
            "rg \"needle\" .": tools.ToolExecutionLane.SHELL_READ,
            "rg foo . | head -n 5": tools.ToolExecutionLane.SHELL_READ,
            "Get-Content a.txt | Select-String foo": tools.ToolExecutionLane.SHELL_READ,
            "git status --short": tools.ToolExecutionLane.SHELL_READ,
            "pytest tests": tools.ToolExecutionLane.SHELL_VERIFY,
            "pytest tests | Select-Object -First 10": tools.ToolExecutionLane.SHELL_VERIFY,
            "ruff check .": tools.ToolExecutionLane.SHELL_VERIFY,
            "npm run dev": tools.ToolExecutionLane.SHELL_LONG_RUNNING,
            "pnpm dev": tools.ToolExecutionLane.SHELL_LONG_RUNNING,
            "python manage.py runserver": tools.ToolExecutionLane.SHELL_LONG_RUNNING,
            "uvicorn app:app": tools.ToolExecutionLane.SHELL_LONG_RUNNING,
            "cd web && npm run dev": tools.ToolExecutionLane.SHELL_LONG_RUNNING,
            "npm run test": tools.ToolExecutionLane.SHELL_SERIAL,
            "make test": tools.ToolExecutionLane.SHELL_SERIAL,
            "python script.py": tools.ToolExecutionLane.SHELL_SERIAL,
            "cd src; pwd": tools.ToolExecutionLane.SHELL_SERIAL,
            "export FOO=bar && npm run dev": tools.ToolExecutionLane.SHELL_SERIAL,
            "conda activate base": tools.ToolExecutionLane.SHELL_SERIAL,
            "cat > file.txt": tools.ToolExecutionLane.SHELL_SERIAL,
            "rg needle . > out.txt": tools.ToolExecutionLane.SHELL_SERIAL,
            "pytest tests > result.txt": tools.ToolExecutionLane.SHELL_SERIAL,
            "rg foo . | tee out.txt": tools.ToolExecutionLane.SHELL_SERIAL,
        }

        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(classify_shell_lane(command), expected)

    def test_shell_lane_uses_shared_stateful_classifier(self):
        from harness_code_agent.agent.tool_executor import classify_shell_lane

        with patch("harness_code_agent.runtime.shell_classification.contains_stateful_shell_operation", return_value=True):
            self.assertEqual(classify_shell_lane("rg needle ."), tools.ToolExecutionLane.SHELL_SERIAL)

    def test_parallel_read_tools_finish_faster_but_results_keep_original_order(self):
        registry = tools.ToolRegistry()

        def slow_tool(label, delay=0.2):
            time.sleep(delay)
            return ToolResult(tool="slow_read", status="success", output=f"done {label}")

        registry.register(_schema("slow_read"), slow_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        tool_calls = [
            _tool_call("tc_a", "slow_read", {"label": "a", "delay": 0.25}),
            _tool_call("tc_b", "slow_read", {"label": "b", "delay": 0.25}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, context = _conversation_with_registry(Path(tmp), registry, tool_calls)
            start = time.perf_counter()
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()
            elapsed = time.perf_counter() - start

        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertLess(elapsed, 0.45)
        self.assertEqual([msg["tool_call_id"] for msg in tool_messages], ["tc_a", "tc_b"])
        self.assertIn("done a", tool_messages[0]["content"])
        self.assertIn("done b", tool_messages[1]["content"])
        self.assertEqual([event.payload["tool"] for event in context.event_bus.events if event.type == "tool_call"], ["slow_read", "slow_read"])

    def test_parallel_workers_do_not_emit_events_before_main_thread_ordering(self):
        registry = tools.ToolRegistry()

        def timed_tool(label, delay):
            time.sleep(delay)
            return ToolResult(tool="timed_read", status="success", output=label)

        registry.register(_schema("timed_read"), timed_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        tool_calls = [
            _tool_call("tc_slow", "timed_read", {"label": "slow", "delay": 0.25}),
            _tool_call("tc_fast", "timed_read", {"label": "fast", "delay": 0.01}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, context = _conversation_with_registry(Path(tmp), registry, tool_calls)
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        result_outputs = [
            event.payload["output"]
            for event in context.event_bus.events
            if event.type == "tool_result"
        ]
        self.assertEqual(len(conversation.observation_store.observations), 2)
        self.assertIn("slow", result_outputs[0])
        self.assertIn("fast", result_outputs[1])

    def test_tool_search_reveals_deferred_schema_for_next_iteration(self):
        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        registry.register(
            {
                "type": "function",
                "function": {
                    "name": "mcp__docs__search",
                    "description": "Search product documentation",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Documentation query"}
                        },
                    },
                },
            },
            lambda **_: ToolResult(tool="mcp__docs__search", status="success", output="searched"),
            permission="read",
            disclosure="deferred",
        )
        tool_calls = [
            _tool_call("tc_search", "tool_search", {"query": "product documentation"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="danger-full-access"),
                event_bus=EventBus(),
                tool_registry=registry,
                allowed_tool_permissions={"read", "network_read", "edit", "control", "shell"},
                blocked_tool_names={"browser_test", "stop_dev_server"},
                revealed_tool_names=set(),
            )
            initial_schemas = tools.tool_schemas_for_profile(
                allowed_permissions=context.allowed_tool_permissions,
                exclude_names=context.blocked_tool_names,
                registry=registry,
            )
            fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(tool_calls)))
            with patch("harness_code_agent.agent.conversation.get_client", return_value=fake_client):
                agent = Agent(
                    "main_agent",
                    "system",
                    use_tools=True,
                    tool_schemas=initial_schemas,
                    tool_context=context,
                )
                conversation = AgentConversation(agent)
                agent._conversations.add(conversation)
                conversation._cached_prompt_cache_key = "old-cache-key"
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        names = {schema["function"]["name"] for schema in agent.tool_schemas}
        self.assertIn("tool_search", names)
        self.assertIn("mcp__docs__search", names)
        self.assertIn("mcp__docs__search", agent.allowed_tool_names)
        self.assertEqual(context.revealed_tool_names, {"mcp__docs__search"})
        self.assertIsNone(conversation._cached_prompt_cache_key)

    def test_repeated_tool_search_reveal_preserves_prompt_cache_when_unchanged(self):
        from harness_code_agent.agent.tool_executor import ToolExecutor

        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        registry.register(
            {
                "type": "function",
                "function": {
                    "name": "mcp__docs__search",
                    "description": "Search product documentation",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            lambda **_: "ok",
            permission="read",
            disclosure="deferred",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="danger-full-access"),
                event_bus=EventBus(),
                tool_registry=registry,
                allowed_tool_permissions={"read"},
                blocked_tool_names=set(),
                revealed_tool_names={"mcp__docs__search"},
            )
            agent = Agent(
                "main_agent",
                "system",
                use_tools=True,
                tool_schemas=tools.tool_schemas_for_profile(
                    allowed_permissions={"read"},
                    registry=registry,
                ) + tools.tool_schemas_for_profile(
                    allowed_permissions={"read"},
                    registry=registry,
                    disclosure={"deferred"},
                    include_names={"mcp__docs__search"},
                ),
                tool_context=context,
            )
            conversation = AgentConversation(agent)
            conversation._cached_prompt_cache_key = "old-cache-key"
            executor = ToolExecutor(conversation)
            result = ToolResult(
                tool="tool_search",
                status="success",
                output="already revealed",
                metadata={"revealed_tool_names": ["mcp__docs__search"]},
            )

            executor._reveal_tool_schemas_from_result(result)

        self.assertEqual(conversation._cached_prompt_cache_key, "old-cache-key")

    def test_write_barrier_prevents_later_read_from_running_before_write(self):
        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        tool_calls = [
            _tool_call("tc_read_before", "read_file", {"path": "note.txt"}),
            _tool_call("tc_write", "write_file", {"path": "note.txt", "content": "after"}),
            _tool_call("tc_read_after", "read_file", {"path": "note.txt"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("before", encoding="utf-8")
            conversation, _context = _conversation_with_registry(root, registry, tool_calls)
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertIn("before", tool_messages[0]["content"])
        self.assertIn("Wrote", tool_messages[1]["content"])
        self.assertIn("after", tool_messages[2]["content"])

    def test_before_tool_block_only_blocks_current_tool_in_parallel_group(self):
        registry = tools.ToolRegistry()

        def read_tool(label):
            return ToolResult(tool="parallel_read", status="success", output=f"ok {label}")

        registry.register(_schema("parallel_read"), read_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)

        class BlockMiddle(AgentMiddleware):
            def before_tool(self, tool_name, tool_args, messages, runtime_state=None, agent_name=None):
                if tool_args.get("label") == "b":
                    return "[blocked] blocked b"
                return None

        tool_calls = [
            _tool_call("tc_a", "parallel_read", {"label": "a"}),
            _tool_call("tc_b", "parallel_read", {"label": "b"}),
            _tool_call("tc_c", "parallel_read", {"label": "c"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, _context = _conversation_with_registry(Path(tmp), registry, tool_calls, middlewares=[BlockMiddle()])
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertIn("ok a", tool_messages[0]["content"])
        self.assertIn("blocked b", tool_messages[1]["content"])
        self.assertIn("ok c", tool_messages[2]["content"])

    def test_before_tool_fallback_blocks_later_unstarted_parallel_tools(self):
        registry = tools.ToolRegistry()
        executed_labels = []

        def read_tool(label):
            executed_labels.append(label)
            return ToolResult(tool="parallel_read", status="success", output=f"ok {label}")

        registry.register(_schema("parallel_read"), read_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)

        class FallbackMiddle(AgentMiddleware):
            def before_tool(self, tool_name, tool_args, messages, runtime_state=None, agent_name=None):
                if tool_args.get("label") == "b":
                    runtime_state.fallback.request_stop(reason="test_fallback", last_tool=tool_name)
                    return "[blocked] stopping at b"
                return None

        tool_calls = [
            _tool_call("tc_a", "parallel_read", {"label": "a"}),
            _tool_call("tc_b", "parallel_read", {"label": "b"}),
            _tool_call("tc_c", "parallel_read", {"label": "c"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, _context = _conversation_with_registry(Path(tmp), registry, tool_calls, middlewares=[FallbackMiddle()])
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertEqual(executed_labels, ["a"])
        self.assertIn("ok a", tool_messages[0]["content"])
        self.assertIn("stopping at b", tool_messages[1]["content"])
        self.assertIn("Agent fallback triggered (test_fallback)", tool_messages[2]["content"])

    def test_tool_call_budget_blocks_remaining_parallel_group_calls(self):
        registry = tools.ToolRegistry()
        executed_labels = []

        def read_tool(label):
            executed_labels.append(label)
            return ToolResult(tool="parallel_read", status="success", output=f"ok {label}")

        registry.register(_schema("parallel_read"), read_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        tool_calls = [
            _tool_call("tc_a", "parallel_read", {"label": "a"}),
            _tool_call("tc_b", "parallel_read", {"label": "b"}),
            _tool_call("tc_c", "parallel_read", {"label": "c"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, _context = _conversation_with_registry(Path(tmp), registry, tool_calls)
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_TOTAL_TOKENS", 100),
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_TOOL_CALLS", 1),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                text = conversation.run_until_idle()

        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertEqual(executed_labels, ["a"])
        self.assertIn("Agent fallback triggered", text)
        self.assertEqual([msg["tool_call_id"] for msg in tool_messages], ["tc_a", "tc_b", "tc_c"])
        self.assertIn("ok a", tool_messages[0]["content"])
        self.assertIn("tool_call_budget_exceeded", tool_messages[1]["content"])
        self.assertIn("tool_call_budget_exceeded", tool_messages[2]["content"])

    def test_approval_is_requested_only_when_later_serial_tool_is_reached(self):
        from harness_code_agent.runtime.approvals import ApprovalResult
        from harness_code_agent.runtime.permission_middleware import PermissionMiddleware

        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        tool_calls = [
            _tool_call("tc_write", "write_file", {"path": "note.txt", "content": "created"}),
            _tool_call("tc_risky", "run_bash", {"command": "git add ."}),
        ]

        approval_reads_file = []

        class ApprovalProvider:
            def request(self, request):
                approval_reads_file.append((Path(tmp) / "note.txt").exists())
                return ApprovalResult(approved=False, reason="no", metadata={})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversation, context = _conversation_with_registry(root, registry, tool_calls)
            context.permission_policy = PermissionPolicy(mode="workspace-write")
            context.approval_provider = ApprovalProvider()
            conversation.agent.middlewares.append(PermissionMiddleware(context, registry))
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        self.assertEqual(approval_reads_file, [True])

    def test_parallel_group_waits_for_each_tool_own_timeout_not_shared_group_timeout(self):
        registry = tools.ToolRegistry()
        seen_timeouts = []

        def timed_tool(label, timeout=0):
            seen_timeouts.append((label, timeout))
            time.sleep(0.15 if label == "slow" else 0.01)
            return ToolResult(tool="timeout_read", status="success", output=f"{label}:{timeout}")

        registry.register(_schema("timeout_read"), timed_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        tool_calls = [
            _tool_call("tc_slow", "timeout_read", {"label": "slow", "timeout": 300}),
            _tool_call("tc_fast", "timeout_read", {"label": "fast", "timeout": 30}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, _context = _conversation_with_registry(Path(tmp), registry, tool_calls)
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        self.assertEqual(seen_timeouts, [("slow", 300), ("fast", 30)])
        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertIn("slow:300", tool_messages[0]["content"])
        self.assertIn("fast:30", tool_messages[1]["content"])

    def test_parallel_group_observes_cancellation_while_waiting_for_tools(self):
        from harness_code_agent.agent.cancellation import CancellationToken, CancelledError

        registry = tools.ToolRegistry()
        token = CancellationToken()

        def slow_tool(cancellation_token=None):
            deadline = time.time() + 0.4
            while time.time() < deadline:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    return ToolResult(tool="slow_read", status="failed", output="slow cancelled")
                time.sleep(0.01)
            return ToolResult(tool="slow_read", status="success", output="slow done")

        def cancel_tool():
            token.cancel()
            return ToolResult(tool="cancel_read", status="success", output="cancelled")

        registry.register(_schema("slow_read"), slow_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        registry.register(_schema("cancel_read"), cancel_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        tool_calls = [
            _tool_call("tc_slow", "slow_read"),
            _tool_call("tc_cancel", "cancel_read"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, _context = _conversation_with_registry(Path(tmp), registry, tool_calls)
            start = time.perf_counter()
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                with self.assertRaises(CancelledError):
                    conversation.run_until_idle(cancellation_token=token)
            elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.25)

    def test_parallel_group_passes_cancellation_token_to_tool_handlers(self):
        from harness_code_agent.agent.cancellation import CancellationToken, CancelledError

        registry = tools.ToolRegistry()
        token = CancellationToken()
        seen = []

        def slow_tool(cancellation_token=None):
            seen.append(cancellation_token)
            while not cancellation_token.is_cancelled:
                time.sleep(0.01)
            seen.append("slow_observed_cancel")
            return ToolResult(tool="slow_read", status="failed", output="slow cancelled")

        def cancel_tool(cancellation_token=None):
            token.cancel()
            return ToolResult(tool="cancel_read", status="success", output="cancelled")

        registry.register(_schema("slow_read"), slow_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        registry.register(_schema("cancel_read"), cancel_tool, permission="read", lane=tools.ToolExecutionLane.WORKSPACE_READ)
        tool_calls = [
            _tool_call("tc_slow", "slow_read"),
            _tool_call("tc_cancel", "cancel_read"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            conversation, _context = _conversation_with_registry(Path(tmp), registry, tool_calls)
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                with self.assertRaises(CancelledError):
                    conversation.run_until_idle(cancellation_token=token)

        self.assertIs(seen[0], token)
        self.assertIn("slow_observed_cancel", seen)

    def test_long_running_shell_is_serial_barrier_and_returns_job_id(self):
        registry = tools.BUILTIN_TOOL_REGISTRY.copy()
        tool_calls = [
            _tool_call("tc_before", "read_file", {"path": "note.txt"}),
            _tool_call("tc_long", "run_bash", {"command": "npm run dev"}),
            _tool_call("tc_logs", "read_shell_output", {"job_id": "shell-job-test"}),
        ]

        class FakeJobs:
            def __init__(self):
                self.started = []
                self.closed = False

            def start(self, command, *, early_exit_seconds=0.5):
                self.started.append((command, early_exit_seconds))
                return SimpleNamespace(
                    job_id="shell-job-test",
                    command=command,
                    pid=123,
                    status="running",
                    exit_code=None,
                    output_tail="",
                )

            def read_output(self, job_id, max_chars=12000):
                return "server ready"

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("before", encoding="utf-8")
            conversation, _context = _conversation_with_registry(root, registry, tool_calls)
            fake_jobs = FakeJobs()
            conversation.runtime_state.shell_job_manager = fake_jobs
            with (
                patch("harness_code_agent.agent.conversation.config.MAX_AGENT_ITERATIONS", 2),
                patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=1),
            ):
                conversation.run_until_idle()

        tool_messages = [msg for msg in conversation.messages if msg.get("role") == "tool"]
        self.assertEqual(fake_jobs.started, [("npm run dev", 0.5)])
        self.assertIn("before", tool_messages[0]["content"])
        self.assertIn("shell-job-test", tool_messages[1]["content"])
        self.assertIn("server ready", tool_messages[2]["content"])


if __name__ == "__main__":
    unittest.main()
