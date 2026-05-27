import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


class ProductRuntimeTests(unittest.TestCase):
    def test_deepseek_reasoning_content_round_trips_on_tool_call_assistant_message(self):
        from harness_code_agent.agent.loop import _assistant_message_from_response

        msg = SimpleNamespace(
            content=None,
            reasoning_content="think carefully",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(name="read_file", arguments='{"path":"README.md"}'),
                ),
            ],
        )

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.deepseek.com"),
            patch("harness_code_agent.agent.loop.config.MODEL", "deepseek-v4-flash"),
        ):
            assistant_msg = _assistant_message_from_response(msg)

        self.assertEqual(assistant_msg["reasoning_content"], "think carefully")
        self.assertEqual(assistant_msg["tool_calls"][0]["function"]["name"], "read_file")

    def test_deepseek_reasoning_content_round_trips_from_model_extra(self):
        from harness_code_agent.agent.loop import _assistant_message_from_response

        msg = SimpleNamespace(
            content=None,
            model_extra={"reasoning_content": "provider extra thinking"},
            tool_calls=[],
        )

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.deepseek.com"),
            patch("harness_code_agent.agent.loop.config.MODEL", "deepseek-v4-flash"),
        ):
            assistant_msg = _assistant_message_from_response(msg)

        self.assertEqual(assistant_msg["reasoning_content"], "provider extra thinking")

    def test_non_deepseek_assistant_message_omits_reasoning_content(self):
        from harness_code_agent.agent.loop import _assistant_message_from_response

        msg = SimpleNamespace(content="ok", reasoning_content="hidden", tool_calls=None)

        with (
            patch("harness_code_agent.agent.loop.config.BASE_URL", "https://api.openai.com/v1"),
            patch("harness_code_agent.agent.loop.config.MODEL", "gpt-4o"),
        ):
            assistant_msg = _assistant_message_from_response(msg)

        self.assertNotIn("reasoning_content", assistant_msg)

    def test_provider_auto_detection_distinguishes_openai_deepseek_and_compatible(self):
        from harness_code_agent.agent.providers import resolve_provider_name

        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://api.openai.com/v1", model="gpt-4o"),
            "openai",
        )
        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://api.deepseek.com", model="deepseek-chat"),
            "deepseek",
        )
        self.assertEqual(
            resolve_provider_name(provider="auto", base_url="https://example.invalid/v1", model="custom"),
            "openai-compatible",
        )

    def test_cached_provider_client_refreshes_when_config_changes(self):
        from harness_code_agent.agent import providers

        providers.reset_client()
        created = []

        def fake_openai(**kwargs):
            client = SimpleNamespace(kwargs=kwargs)
            created.append(client)
            return client

        try:
            with (
                patch("harness_code_agent.agent.providers.OpenAI", side_effect=fake_openai),
                patch.object(providers.config, "API_KEY", "key-a"),
                patch.object(providers.config, "BASE_URL", "https://one.example/v1"),
            ):
                first = providers.get_client()

            with (
                patch("harness_code_agent.agent.providers.OpenAI", side_effect=fake_openai),
                patch.object(providers.config, "API_KEY", "key-a"),
                patch.object(providers.config, "BASE_URL", "https://two.example/v1"),
            ):
                second = providers.get_client()
        finally:
            providers.reset_client()

        self.assertIsNot(first, second)
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].kwargs["base_url"], "https://one.example/v1")
        self.assertEqual(created[1].kwargs["base_url"], "https://two.example/v1")

    def test_provider_streaming_normalizes_content_reasoning_and_tool_calls(self):
        from harness_code_agent.agent.providers import ProviderAdapter

        def chunk(delta, finish_reason=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=delta,
                        finish_reason=finish_reason,
                    )
                ]
            )

        deltas = []
        chunks = [
            chunk(SimpleNamespace(content="hel")),
            chunk(SimpleNamespace(content="lo", reasoning_content="think ")),
            chunk(
                SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(name="read_file", arguments='{"pa'),
                        )
                    ]
                )
            ),
            chunk(
                SimpleNamespace(
                    reasoning_content="carefully",
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            function=SimpleNamespace(arguments='th":"README.md"}'),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            ),
        ]

        result = ProviderAdapter("deepseek").assistant_message_from_stream(chunks, on_text_delta=deltas.append)

        self.assertEqual(deltas, ["hel", "lo"])
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.assistant_message["content"], "hello")
        self.assertEqual(result.assistant_message["reasoning_content"], "think carefully")
        self.assertEqual(result.assistant_message["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(result.assistant_message["tool_calls"][0]["function"]["arguments"], '{"path":"README.md"}')

    def test_provider_streaming_checks_cancellation_between_chunks(self):
        from harness_code_agent.agent.cancellation import CancellationToken, CancelledError
        from harness_code_agent.agent.providers import ProviderAdapter

        def chunk(text):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=text),
                        finish_reason=None,
                    )
                ]
            )

        token = CancellationToken()
        deltas = []

        def chunks():
            yield chunk("hel")
            token.cancel()
            yield chunk("lo")

        with self.assertRaises(CancelledError):
            ProviderAdapter("openai-compatible").assistant_message_from_stream(
                chunks(),
                on_text_delta=deltas.append,
                cancellation_token=token,
            )

        self.assertEqual(deltas, ["hel"])

    def test_streaming_request_falls_back_to_non_stream_before_first_chunk(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("stream"):
                    raise RuntimeError("stream unavailable")
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="fallback", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake_client = FakeClient()
        with patch("harness_code_agent.agent.loop.get_client", return_value=fake_client):
            conversation = AgentConversation(Agent("test", "system", use_tools=False, stream_callback=lambda _: None))
            completion = conversation._request_assistant_message(
                conversation.provider.chat_kwargs(model="m", messages=[], max_tokens=10)
            )

        self.assertEqual(completion[0]["content"], "fallback")
        self.assertTrue(fake_client.chat.completions.calls[0]["stream"])
        self.assertNotIn("stream", fake_client.chat.completions.calls[1])

    def test_streaming_request_traces_pre_chunk_fallback_reason(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        class FakeCompletions:
            def create(self, **kwargs):
                if kwargs.get("stream"):
                    raise RuntimeError("stream unavailable")
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="fallback", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            conversation = AgentConversation(Agent("test", "system", use_tools=False, stream_callback=lambda _: None))

        with patch.object(conversation.trace, "error") as trace_error:
            completion = conversation._request_assistant_message(
                conversation.provider.chat_kwargs(model="m", messages=[], max_tokens=10)
            )

        self.assertEqual(completion[0]["content"], "fallback")
        trace_error.assert_called_once()
        self.assertEqual(trace_error.call_args.args[0], "stream_fallback")
        self.assertIn("stream unavailable", trace_error.call_args.args[1])

    def test_streaming_request_collects_text_deltas_without_non_stream_fallback(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation

        def chunk(text, finish_reason=None):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=text),
                        finish_reason=finish_reason,
                    )
                ]
            )

        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return [chunk("hel"), chunk("lo", finish_reason="stop")]

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        deltas = []
        fake_client = FakeClient()
        with patch("harness_code_agent.agent.loop.get_client", return_value=fake_client):
            conversation = AgentConversation(Agent("test", "system", use_tools=False, stream_callback=deltas.append))
            completion = conversation._request_assistant_message(
                conversation.provider.chat_kwargs(model="m", messages=[], max_tokens=10)
            )

        self.assertEqual(completion[0]["content"], "hello")
        self.assertEqual(completion[1], "stop")
        self.assertEqual(deltas, ["hel", "lo"])
        self.assertTrue(conversation.last_run_streamed_text)
        self.assertEqual(len(fake_client.chat.completions.calls), 1)

    def test_tool_enabled_agent_builds_chat_kwargs_once(self):
        from harness_code_agent.agent.loop import Agent, AgentConversation
        from harness_code_agent.agent.providers import ProviderAdapter

        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="done", tool_calls=None),
                            finish_reason="stop",
                        )
                    ]
                )

        class FakeClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=FakeCompletions())

        class CountingProvider:
            def __init__(self):
                self.calls = []
                self.delegate = ProviderAdapter("openai")

            def chat_kwargs(self, **kwargs):
                self.calls.append(kwargs)
                return self.delegate.chat_kwargs(**kwargs)

            def assistant_message_from_response(self, msg):
                return self.delegate.assistant_message_from_response(msg)

        schema = [{"type": "function", "function": {"name": "read_file"}}]
        provider = CountingProvider()
        with patch("harness_code_agent.agent.loop.get_client", return_value=FakeClient()):
            conversation = AgentConversation(Agent("test", "system", use_tools=True, tool_schemas=schema))
        conversation.provider = provider

        with (
            patch("harness_code_agent.agent.loop.config.MAX_AGENT_ITERATIONS", 1),
            patch("harness_code_agent.agent.loop.context.count_tokens", return_value=1),
        ):
            result = conversation.run_until_idle()

        self.assertEqual(result, "done")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["tools"], schema)
        self.assertEqual(provider.calls[0]["tool_choice"], "auto")

    def test_trace_writer_stores_traces_under_harness_directory_without_stderr_by_default(self):
        from harness_code_agent.agent.loop import TraceWriter

        with tempfile.TemporaryDirectory() as tmp:
            stderr = StringIO()
            with (
                patch("harness_code_agent.agent.loop.config.WORKSPACE", tmp),
                patch("harness_code_agent.agent.loop.config.TRACE_STDERR", False),
            ):
                with redirect_stderr(stderr):
                    writer = TraceWriter("main_agent")
                    writer.iteration(1, 42)

            trace_path = Path(tmp) / ".harness" / "traces" / "trace_main_agent.jsonl"
            self.assertTrue(trace_path.exists())
            self.assertFalse((Path(tmp) / "_trace_main_agent.jsonl").exists())
            self.assertEqual(stderr.getvalue(), "")

    def test_builtin_tool_registry_exposes_schema_and_dispatch_exports(self):
        from harness_code_agent.runtime import tools

        registry_names = {
            schema["function"]["name"]
            for schema in tools.BUILTIN_TOOL_REGISTRY.schemas()
        }
        exported_schema_names = {
            schema["function"]["name"]
            for schema in tools.TOOL_SCHEMAS + tools.BROWSER_TOOL_SCHEMAS
        }

        self.assertEqual(registry_names, exported_schema_names)
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("read_file"), tools.TOOL_DISPATCH["read_file"])
        self.assertIs(tools.BUILTIN_TOOL_REGISTRY.get("stop_dev_server"), tools.TOOL_DISPATCH["stop_dev_server"])
        self.assertIsNone(tools.BUILTIN_TOOL_REGISTRY.get("missing_tool"))

    def test_structured_event_schema_covers_mvp_event_types(self):
        from harness_code_agent.sessions.events import (
            AssistantMessageEvent,
            FailureEvent,
            FileChangeEvent,
            FinalReportEvent,
            SessionFinishedEvent,
            TaskOutcomeEvent,
            ToolCallEvent,
            ToolResultEvent,
            UserInputEvent,
        )

        event_types = {
            UserInputEvent(text="fix").to_event().type,
            AssistantMessageEvent(text="done").to_event().type,
            ToolCallEvent(tool="read_file", args={"path": "README.md"}).to_event().type,
            ToolResultEvent(tool="read_file", status="success", output="ok").to_event().type,
            FileChangeEvent(path="app.py").to_event().type,
            FailureEvent(category="tool_error", message="boom").to_event().type,
            FinalReportEvent(status="success", reason="completed", summary="done").to_event().type,
            SessionFinishedEvent(reason="user_exit", status="closed").to_event().type,
            TaskOutcomeEvent(status="success", evidence=["tests_passed"], summary="done").to_event().type,
        }

        self.assertEqual(event_types, {
            "user_input",
            "assistant_message",
            "tool_call",
            "tool_result",
            "file_change",
            "failure",
            "final_report",
            "session_finished",
            "task_outcome",
        })

    def test_failure_classification_uses_stable_sources_before_text(self):
        from harness_code_agent.runtime.tool_result import ToolResult
        from harness_code_agent.sessions.events import classify_tool_failure

        cases = [
            (
                ToolResult(tool="read_file", status="failed", error="missing", metadata={"status_source": "native"}),
                "tool_error",
            ),
            (
                ToolResult(tool="write_file", status="failed", error="empty path", metadata={"status_source": "validation"}),
                "validation_error",
            ),
            (
                ToolResult(tool="run_bash", status="failed", error="Command exited with code 1", metadata={"status_source": "shell"}),
                "runtime_error",
            ),
            (
                ToolResult(tool="write_file", status="failed", error="user said no", metadata={"status_source": "approval"}),
                "user_cancelled",
            ),
            (
                ToolResult(tool="custom", status="failed", error="", metadata={}),
                "unknown",
            ),
        ]

        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_tool_failure(result), expected)

    def test_tool_result_serializes_and_tool_execution_records_structured_events(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.runtime.tool_result import ToolResult
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        result = ToolResult(
            tool="read_file",
            status="failed",
            output="",
            error="missing",
            return_code=2,
            metadata={"path": "missing.txt"},
        )

        self.assertEqual(result.to_dict()["tool"], "read_file")
        self.assertEqual(result.to_dict()["status"], "failed")
        self.assertFalse(result.to_dict()["ok"])
        self.assertEqual(result.to_dict()["error"], "missing")
        self.assertEqual(result.to_text(), "[error] missing")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            output = tools.execute_tool(
                "read_file",
                {"path": "note.txt"},
                tool_context=context,
                agent_name="main_agent",
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(output, "hello")
            event_types = [event["type"] for event in events]
            self.assertEqual(event_types, ["tool_call", "tool_result"])
            tool_result = next(event for event in events if event["type"] == "tool_result")
            self.assertEqual(tool_result["payload"]["tool"], "read_file")
            self.assertEqual(tool_result["payload"]["status"], "success")
            self.assertTrue(tool_result["payload"]["ok"])
            self.assertEqual(tool_result["payload"]["output"], "[redacted read_file output: 5 chars]")
            self.assertTrue(tool_result["payload"]["metadata"]["output_redacted"])
            self.assertEqual(tool_result["payload"]["metadata"]["output_length"], 5)

    def test_tool_result_does_not_infer_status_from_raw_tool_text(self):
        from unittest.mock import patch

        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            with patch.object(
                tools.BUILTIN_TOOL_REGISTRY,
                "get",
                return_value=lambda **kwargs: "[error] this is domain output, not execution status",
            ):
                output = tools.execute_tool(
                    "custom_tool",
                    {},
                    tool_context=context,
                    agent_name="main_agent",
                )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            tool_result = [event for event in events if event["type"] == "tool_result"][0]

            self.assertEqual(output, "[error] this is domain output, not execution status")
            self.assertEqual(tool_result["payload"]["status"], "unknown")
            self.assertIsNone(tool_result["payload"]["ok"])
            self.assertEqual(tool_result["payload"]["metadata"]["status_source"], "unstructured")
            self.assertFalse(any(event["type"] == "failure" for event in events))

    def test_unknown_tool_records_structured_failure_events(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            output = tools.execute_tool(
                "missing_tool",
                {"secret": "nope"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            event_types = [event["type"] for event in events]
            tool_result = next(event for event in events if event["type"] == "tool_result")

            self.assertEqual(output, "[error] Unknown tool: missing_tool")
            self.assertIn("tool_call", event_types)
            self.assertIn("failure", event_types)
            self.assertEqual(tool_result["payload"]["status"], "failed")
            self.assertFalse(tool_result["payload"]["ok"])
            failure = next(event for event in events if event["type"] == "failure")
            self.assertEqual(failure["payload"]["category"], "tool_error")
            self.assertEqual(failure["payload"]["tool"], "missing_tool")

    def test_tool_validation_failures_return_typed_failed_results(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            missing = tools.execute_tool(
                "read_file",
                {"path": "missing.txt"},
                tool_context=context,
                agent_name="main_agent",
            )
            empty_write = tools.execute_tool(
                "write_file",
                {"path": "", "content": "x"},
                tool_context=context,
                agent_name="main_agent",
            )
            empty_patch = tools.execute_tool(
                "apply_patch",
                {"path": "", "search": "x", "replace": "y"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            failed_results = [
                event for event in events
                if event["type"] == "tool_result"
                and event["payload"].get("status") == "failed"
            ]

            self.assertIn("[error] File not found: missing.txt", missing)
            self.assertIn("[auto-fix] Empty file path", empty_write)
            self.assertIn("[error] Empty file path", empty_patch)
            self.assertEqual(len(failed_results), 3)
            failures = [event for event in events if event["type"] == "failure"]
            self.assertEqual(len(failures), 3)
            self.assertEqual(
                [event["payload"]["category"] for event in failures],
                ["tool_error", "validation_error", "validation_error"],
            )

    def test_session_store_creates_metadata_and_jsonl_events(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            session = store.create(
                profile="terminal",
                cwd=Path(tmp),
                model="test-model",
                permission_mode="workspace-write",
            )
            bus = store.event_bus(session)
            bus.emit("session_started", agent="main_agent", payload={"task": "fix bug"})

            metadata = json.loads(session.metadata_path.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in session.events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(metadata["profile"], "terminal")
            self.assertEqual(metadata["model"], "test-model")
            self.assertEqual(metadata["permission_mode"], "workspace-write")
            self.assertEqual(events[0]["type"], "session_started")
            self.assertEqual(events[0]["sequence"], 1)

    def test_session_store_lists_and_reads_sessions(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            first = store.create(
                profile="terminal",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            second = store.create(
                profile="plan",
                cwd=Path(tmp),
                model="model-b",
                permission_mode="read-only",
            )
            store.event_bus(second).emit("session_finished", agent="main_agent", payload={})

            sessions = store.list_sessions()
            metadata = store.read_metadata(second.id)
            events = store.read_events(second.id)

            self.assertEqual([item["id"] for item in sessions], [second.id, first.id])
            self.assertEqual(metadata["profile"], "plan")
            self.assertEqual(events[0]["type"], "session_finished")

    def test_session_store_forks_session_metadata_and_lineage_event(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            source = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            store.event_bus(source).emit("session_started", agent="main_agent", payload={})
            store.event_bus(source).emit("session_finished", agent="main_agent", payload={})

            fork = store.fork(source.id)
            metadata = store.read_metadata(fork.id)
            events = store.read_events(fork.id)

            self.assertNotEqual(fork.id, source.id)
            self.assertEqual(metadata["profile"], "coding-agent")
            self.assertEqual(metadata["model"], "model-a")
            self.assertEqual(metadata["permission_mode"], "workspace-write")
            self.assertEqual(metadata["forked_from"], source.id)
            self.assertEqual(metadata["forked_from_event_count"], 2)
            self.assertEqual(events[0]["type"], "session_forked")
            self.assertEqual(events[0]["payload"]["source_session_id"], source.id)

    def test_session_store_reads_fork_lineage_and_resumed_metadata(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            source = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            fork = store.fork(source.id)
            resumed = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
                resumed_from=fork.id,
            )

            lineage = store.read_lineage(fork.id)
            resumed_metadata = store.read_metadata(resumed.id)

            self.assertEqual([item["id"] for item in lineage], [source.id, fork.id])
            self.assertEqual(resumed_metadata["resumed_from"], fork.id)

    def test_session_store_reads_latest_session_and_persisted_summary(self):
        from harness_code_agent.sessions.store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / ".harness")
            first = store.create(
                profile="coding-agent",
                cwd=Path(tmp),
                model="model-a",
                permission_mode="workspace-write",
            )
            second = store.create(
                profile="plan",
                cwd=Path(tmp),
                model="model-b",
                permission_mode="read-only",
            )
            store.event_bus(second).emit("user_input", agent="main_agent", payload={"text": "plan it"})
            summary = store.write_summary(second.id)

            latest = store.latest_session()

            self.assertEqual(latest["id"], second.id)
            self.assertIn("Session summary", summary)
            self.assertIn("profile: plan", store.read_summary(second.id))
            with self.assertRaises(FileNotFoundError):
                store.read_summary(first.id)

    def test_session_summary_formats_human_readable_event_overview(self):
        from harness_code_agent.sessions.summary import format_session_summary

        metadata = {
            "id": "session-a",
            "profile": "coding-agent",
            "model": "model-a",
            "permission_mode": "workspace-write",
            "status": "running",
            "cwd": "C:/workspace",
            "created_at": "2026-05-20T00:00:00+00:00",
            "forked_from": "session-parent",
        }
        events = [
            {"sequence": 1, "type": "session_started", "agent": "main_agent", "payload": {}},
            {"sequence": 2, "type": "turn_started", "agent": "main_agent", "payload": {"turn": 1}},
            {"sequence": 3, "type": "tool_result", "agent": "main_agent", "payload": {"tool": "write_file", "status": "success", "ok": True}},
            {"sequence": 4, "type": "file_change", "agent": "main_agent", "payload": {"path": "app.py"}},
            {"sequence": 5, "type": "approval_requested", "agent": "main_agent", "payload": {"tool": "run_bash"}},
            {"sequence": 6, "type": "approval_decided", "agent": "main_agent", "payload": {"tool": "run_bash", "approved": False}},
            {
                "sequence": 7,
                "type": "profile_switched",
                "agent": "main_agent",
                "payload": {"previous_profile": "coding-agent", "profile": "plan", "reason": "slash command"},
            },
            {"sequence": 8, "type": "plan_ready", "agent": "main_agent", "payload": {"profile": "plan"}},
            {"sequence": 9, "type": "task_outcome", "agent": "main_agent", "payload": {"status": "success", "summary": "done"}},
            {"sequence": 10, "type": "session_finished", "agent": "main_agent", "payload": {"status": "closed", "reason": "user_exit"}},
        ]

        summary = format_session_summary(metadata, events)

        self.assertIn("Session summary", summary)
        self.assertIn("id: session-a", summary)
        self.assertIn("status: closed", summary)
        self.assertIn("forked_from: session-parent", summary)
        self.assertIn("turns: 1 started, 0 finished", summary)
        self.assertIn("tools: 1 call(s): write_file=1", summary)
        self.assertIn("changed_files: app.py", summary)
        self.assertIn("approvals: 1 requested, 0 approved, 1 denied", summary)
        self.assertIn("profile_switches: coding-agent -> plan (slash command)", summary)
        self.assertIn("plans_ready: 1", summary)
        self.assertIn("task_outcome: success - done", summary)
        self.assertIn("recent_events:", summary)

    def test_session_summary_uses_final_report_for_phase_two_status_and_categories(self):
        from harness_code_agent.sessions.summary import format_session_summary

        metadata = {"id": "session-final", "profile": "coding-agent", "status": "running"}
        events = [
            {"sequence": 1, "type": "user_input", "agent": "main_agent", "payload": {"text": "fix"}},
            {"sequence": 2, "type": "tool_result", "agent": "main_agent", "payload": {"tool": "read_file", "status": "failed"}},
            {"sequence": 3, "type": "failure", "agent": "main_agent", "payload": {"category": "tool_error", "message": "missing"}},
            {
                "sequence": 4,
                "type": "final_report",
                "agent": "main_agent",
                "payload": {
                    "status": "failed",
                    "reason": "verification_failed",
                    "summary": "tests still fail",
                    "statistics": {
                        "events": 3,
                        "user_inputs": 1,
                        "assistant_messages": 0,
                        "tool_calls": 1,
                        "failures": 1,
                        "file_changes": 0,
                    },
                    "failure_categories": {"tool_error": 1},
                    "tool_counts": {"read_file": 1},
                    "changed_files": [],
                },
            },
        ]

        summary = format_session_summary(metadata, events)

        self.assertIn("status: failed", summary)
        self.assertIn("final_report: failed - tests still fail", summary)
        self.assertIn("failure_categories: tool_error=1", summary)

    def test_session_summary_handles_empty_or_sparse_events(self):
        from harness_code_agent.sessions.summary import format_session_summary

        summary = format_session_summary(
            {"id": "empty-session", "profile": "plan", "status": "running"},
            [{"type": "tool_result", "payload": None}],
        )

        self.assertIn("id: empty-session", summary)
        self.assertIn("profile: plan", summary)
        self.assertIn("events: 1", summary)
        self.assertIn("tools: 1 call(s): unknown=1", summary)
        self.assertIn("changed_files: unknown", summary)
        self.assertIn("task_outcome: unknown", summary)

    def test_final_report_payload_is_statistics_ready_for_replay_and_evaluation(self):
        from harness_code_agent.sessions.events import FinalReportEvent
        from harness_code_agent.sessions.report import build_final_report

        metadata = {"id": "session-a", "created_at": "2026-05-21T00:00:00+00:00"}
        events = [
            {"sequence": 1, "type": "user_input", "agent": "main_agent", "payload": {"text": "fix"}},
            {"sequence": 2, "type": "assistant_message", "agent": "main_agent", "payload": {"text": "I changed app.py"}},
            {"sequence": 3, "type": "tool_result", "agent": "main_agent", "payload": {"tool": "write_file", "status": "success"}},
            {"sequence": 4, "type": "file_change", "agent": "main_agent", "payload": {"path": "app.py"}},
            {"sequence": 5, "type": "failure", "agent": "main_agent", "payload": {"category": "validation_error", "message": "empty"}},
        ]

        report = build_final_report(
            metadata,
            events,
            status="closed",
            reason="user_exit",
            summary="I changed app.py",
        )
        event = FinalReportEvent(**report).to_event()

        self.assertEqual(event.type, "final_report")
        self.assertEqual(event.payload["session_id"], "session-a")
        self.assertEqual(event.payload["status"], "closed")
        self.assertEqual(event.payload["reason"], "user_exit")
        self.assertEqual(event.payload["summary"], "I changed app.py")
        self.assertEqual(event.payload["statistics"]["user_inputs"], 1)
        self.assertEqual(event.payload["statistics"]["assistant_messages"], 1)
        self.assertEqual(event.payload["statistics"]["tool_calls"], 1)
        self.assertEqual(event.payload["statistics"]["failures"], 1)
        self.assertEqual(event.payload["failure_categories"], {"validation_error": 1})
        self.assertEqual(event.payload["tool_counts"], {"write_file": 1})
        self.assertEqual(event.payload["changed_files"], ["app.py"])

    def test_session_report_and_summary_share_event_helpers(self):
        from harness_code_agent.sessions import _event_helpers
        from harness_code_agent.sessions import report, summary

        self.assertIs(report._count_events, _event_helpers.count_events)
        self.assertIs(report._tool_counts, _event_helpers.tool_counts)
        self.assertIs(report._failure_categories, _event_helpers.failure_categories)
        self.assertIs(report._changed_files, _event_helpers.changed_files)
        self.assertIs(report._event_type, _event_helpers.event_type)
        self.assertIs(report._payload, _event_helpers.payload)
        self.assertIs(summary._count_events, _event_helpers.count_events)
        self.assertIs(summary._tool_counts, _event_helpers.tool_counts)
        self.assertIs(summary._event_failure_categories, _event_helpers.failure_categories)
        self.assertIs(summary._changed_files, _event_helpers.changed_files)
        self.assertIs(summary._event_type, _event_helpers.event_type)
        self.assertIs(summary._payload, _event_helpers.payload)

    def test_workspace_service_resolves_paths_and_snapshots_before_write(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "src" / "app.py"
            target.parent.mkdir()
            target.write_text("old", encoding="utf-8")

            result = workspace.write_text("src/app.py", "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertTrue(result.snapshot_path.exists())
            self.assertEqual(result.snapshot_path.read_text(encoding="utf-8"), "old")
            with self.assertRaises(ValueError):
                workspace.resolve("../outside.txt")

    def test_workspace_service_applies_unique_text_patch_and_rejects_ambiguous_patch(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "app.py"
            target.write_text("alpha\nbeta\n", encoding="utf-8")

            result = workspace.apply_text_patch("app.py", search="beta\n", replace="gamma\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\ngamma\n")
            self.assertTrue(result.snapshot_path.exists())

            target.write_text("same\nsame\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                workspace.apply_text_patch("app.py", search="same\n", replace="once\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "same\nsame\n")

    def test_workspace_service_rolls_back_latest_snapshot_for_file(self):
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            target = root / "app.py"
            target.write_text("old\n", encoding="utf-8")
            workspace.write_text("app.py", "new\n")

            result = workspace.rollback_latest_snapshot("app.py")

            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
            self.assertTrue(result.snapshot_path.exists())

    def test_permission_policy_uses_codex_sandbox_modes(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        read_only_policy = PermissionPolicy(mode="read-only")
        read_decision = read_only_policy.decide_tool_call("read_file", {"path": "x.txt"})
        write_decision = read_only_policy.decide_tool_call("write_file", {"path": "x.txt"})
        shell_decision = read_only_policy.decide_tool_call(
            "run_bash",
            {"command": "git status --short"},
        )

        workspace_policy = PermissionPolicy(mode="workspace-write")
        edit_decision = workspace_policy.decide_tool_call("write_file", {"path": "x.txt"})
        safe_shell_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "git status --short"},
        )
        dangerous_decision = workspace_policy.decide_tool_call(
            "run_bash",
            {"command": "rm -rf build"},
        )

        full_access_policy = PermissionPolicy(mode="danger-full-access")
        full_access_decision = full_access_policy.decide_tool_call(
            "run_bash",
            {"command": "git reset --hard"},
        )

        self.assertTrue(read_decision.allowed)
        self.assertTrue(write_decision.requires_approval)
        self.assertTrue(shell_decision.requires_approval)
        self.assertTrue(edit_decision.allowed)
        self.assertTrue(safe_shell_decision.allowed)
        self.assertTrue(dangerous_decision.requires_approval)
        self.assertEqual(dangerous_decision.risk, "shell_dangerous")
        self.assertTrue(full_access_decision.allowed)

    def test_permission_policy_rejects_unknown_mode_names(self):
        from harness_code_agent.runtime.permissions import PermissionPolicy

        with self.assertRaises(ValueError):
            PermissionPolicy(mode="unsupported-mode")

    def test_execute_tool_with_context_records_events_snapshots_and_approval_denial(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("old", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            workspace = WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots")
            context = ToolContext(
                workspace=workspace,
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            result = tools.execute_tool(
                "write_file",
                {"path": "note.txt", "content": "new"},
                tool_context=context,
                agent_name="main_agent",
            )
            approval_denied = tools.execute_tool(
                "run_bash",
                {"command": "rm -rf build"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Wrote", result)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(len(list((root / ".harness" / "snapshots").rglob("*.*"))), 1)
            self.assertIn("[approval_denied]", approval_denied)
            event_types = [event["type"] for event in events]
            self.assertIn("tool_call", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("file_change", event_types)
            self.assertIn("failure", event_types)
            self.assertNotIn("before_tool", event_types)
            self.assertNotIn("permission_decided", event_types)
            self.assertNotIn("after_tool", event_types)
            self.assertNotIn("file_changed", event_types)
            denied_result = [
                event for event in events
                if event["type"] == "tool_result" and event["payload"].get("tool") == "run_bash"
            ][0]
            self.assertEqual(denied_result["payload"]["status"], "failed")
            self.assertFalse(denied_result["payload"]["ok"])
            approval = [
                event for event in events
                if event["type"] == "approval_decided" and event["payload"].get("tool") == "run_bash"
            ][0]
            self.assertFalse(approval["payload"]["approved"])

    def test_execute_tool_apply_patch_records_snapshot_and_rejects_ambiguous_patch(self):
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("old\n", encoding="utf-8")
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=EventBus(events_path),
            )

            result = tools.execute_tool(
                "apply_patch",
                {"path": "note.txt", "search": "old\n", "replace": "new\n"},
                tool_context=context,
                agent_name="main_agent",
            )
            ambiguous = tools.execute_tool(
                "apply_patch",
                {"path": "note.txt", "search": "", "replace": "x"},
                tool_context=context,
                agent_name="main_agent",
            )

            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Patched note.txt", result)
            self.assertIn("[error] ValueError", ambiguous)
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "new\n")
            self.assertTrue(any(event["type"] == "file_change" for event in events))
            self.assertFalse(any(event["type"] == "file_changed" for event in events))

    def test_execute_tool_runs_approved_tool_call(self):
        from harness_code_agent.runtime.approvals import StaticApprovalProvider
        from harness_code_agent.sessions.events import EventBus
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.workspace.service import WorkspaceService
        from harness_code_agent.runtime import tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / ".harness" / "events.jsonl"
            context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=root / ".harness" / "snapshots"),
                permission_policy=PermissionPolicy(mode="read-only"),
                event_bus=EventBus(events_path),
                approval_provider=StaticApprovalProvider(approved=True, reason="test approval"),
            )

            result = tools.execute_tool(
                "write_file",
                {"path": "approved.txt", "content": "ok"},
                tool_context=context,
                agent_name="main_agent",
            )
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("Wrote", result)
            self.assertEqual((root / "approved.txt").read_text(encoding="utf-8"), "ok")
            requested = [event for event in events if event["type"] == "approval_requested"][0]
            decided = [event for event in events if event["type"] == "approval_decided"][0]
            tool_result = [event for event in events if event["type"] == "tool_result"][0]
            self.assertEqual(requested["payload"]["tool"], "write_file")
            self.assertEqual(decided["payload"]["tool"], "write_file")
            self.assertTrue(decided["payload"]["approved"])
            self.assertEqual(tool_result["payload"]["status"], "success")
            self.assertTrue(tool_result["payload"]["ok"])


if __name__ == "__main__":
    unittest.main()


