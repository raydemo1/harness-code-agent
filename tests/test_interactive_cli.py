import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock, patch


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent import config
from harness_code_agent.core.git_helpers import capture_git_baseline, git_dirty_paths
from harness_code_agent.core.interactive import InteractiveSession
from harness_code_agent.core.mentions import (
    MentionResolutionError,
    render_mention_context,
    resolve_mentions,
)
from harness_code_agent.profiles.base import AgentConfig
from harness_code_agent.runtime.middleware import TimeBudgetMiddleware
from harness_code_agent.sessions.events import FileChangeEvent, ToolResultEvent
from harness_code_agent.sessions.store import SessionStore
from harness_code_agent.skills import SkillRegistry


class FakeConversation:
    instances: ClassVar[list] = []
    response_text = "assistant done"

    def __init__(self):
        self.messages = [{"role": "system", "content": "fake"}]
        self.submissions = []
        self.closed = False
        self.response_text = self.__class__.response_text
        self.__class__.instances.append(self)

    def submit(self, task, cancellation_token=None):
        self.submissions.append(task)
        self.messages.append({"role": "user", "content": task})
        self.messages.append({"role": "assistant", "content": self.response_text})
        return self.response_text

    def close(self):
        self.closed = True


class RecordingInteractiveAgent:
    init_args = None
    init_kwargs = None

    def __init__(self, *args, **kwargs):
        self.__class__.init_args = args
        self.__class__.init_kwargs = kwargs
        self.middlewares = kwargs.get("middlewares") or []
        self.tool_schemas = kwargs.get("tool_schemas") or []

    def update_tool_schemas(self, schemas):
        self.tool_schemas = schemas

    def start_conversation(self):
        return FakeConversation()


class EmptySkillRegistry:
    user_commands: ClassVar[list] = []

    def build_catalog_prompt(self):
        return ""

    def build_user_invocation(self, line):
        return None


class InteractiveCliTests(unittest.TestCase):
    def setUp(self):
        self.old_workspace = config.WORKSPACE
        self.old_api_key = config.API_KEY
        self.old_cwd = os.getcwd()
        self.temp_dir = tempfile.mkdtemp()
        self._skill_registry_patcher = patch("harness_code_agent.core.interactive.SkillRegistry", EmptySkillRegistry)
        self._skill_registry_patcher.start()
        config.API_KEY = "test-key"
        FakeConversation.instances = []
        FakeConversation.response_text = "assistant done"
        os.chdir(self.temp_dir)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self._skill_registry_patcher.stop()
        config.WORKSPACE = self.old_workspace
        config.API_KEY = self.old_api_key
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.temp_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _ensure_git(self):
        if not (Path(self.temp_dir) / ".git").exists():
            from harness_code_agent.core.git_helpers import _ensure_git_repository

            _ensure_git_repository(Path(self.temp_dir))

    def _session(self):
        patcher = patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation())
        patcher.start()
        session = InteractiveSession(cwd=self.temp_dir)
        close = session.close

        def close_with_patcher():
            try:
                close()
            finally:
                patcher.stop()

        session.close = close_with_patcher
        return session

    def test_interactive_session_uses_current_directory_workspace(self):
        session = self._session()
        try:
            self.assertEqual(Path(config.WORKSPACE), Path(self.temp_dir))
            self.assertEqual(session.cwd, Path(self.temp_dir).resolve())
            self.assertTrue((Path(self.temp_dir) / ".harness").exists())
        finally:
            session.close()

    def test_interactive_session_starts_bound_to_general_with_session_record(self):
        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()) as start:
            session = InteractiveSession(cwd=self.temp_dir)
        try:
            self.assertTrue(session.is_bound)
            self.assertIsNotNone(session.session)
            self.assertIsNotNone(session.session_id)
            self.assertEqual(session.profile.name(), "general")
            self.assertEqual(session.display_profile, "general")
            metadata = session.session_store.read_metadata(session.session.id)
            self.assertEqual(metadata["profile"], "general")
            self.assertEqual(metadata["initial_profile"], "general")
            self.assertEqual(metadata["profile_source"], "default")
            self.assertEqual(len(session.session_store.list_sessions()), 1)
            start.assert_called_once()
        finally:
            session.close()

    def test_user_prompt_augmentation_middleware_runs_before_submit(self):
        class AugmentMiddleware:
            def augment_user_prompt(
                self,
                user_prompt,
                messages,
                runtime_state=None,
                agent_name=None,
                mention_paths=None,
            ):
                return f"Injected context for: {user_prompt}"

        class FakeAugmentProfile:
            def name(self):
                return "fake-augment"

            def main_agent(self):
                return AgentConfig(system_prompt="fake", middlewares=[AugmentMiddleware()])

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakeAugmentProfile()),
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-augment")
            try:
                session.submit("inspect parser")

                submitted = FakeConversation.instances[-1].submissions[-1]
                self.assertIn("Injected context for: inspect parser", submitted)
                self.assertLess(submitted.index("Injected context"), submitted.index("User turn:"))
            finally:
                session.close()

    def test_large_user_prompt_is_externalized_before_agent_submit(self):
        head = "Task: analyze this pasted payload\n" + ("H" * 2200)
        middle = "MIDDLE-OMITTED-" * 400
        tail = "T" * 2200
        large_prompt = head + middle + tail

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch.dict("os.environ", {"HARNESS_TURN_INLINE_CHAR_LIMIT": "100"}),
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                session.submit(large_prompt)

                submitted = FakeConversation.instances[-1].submissions[-1]
                input_files = list((session.session.root / "inputs").glob("turn-0001-prompt*.txt"))
                self.assertEqual(len(input_files), 1)
                self.assertEqual(input_files[0].read_text(encoding="utf-8"), large_prompt)
                self.assertIn("[EXTERNALIZED TURN CONTENT]", submitted)
                self.assertIn(str(input_files[0]), submitted)
                self.assertIn("Use read_file to inspect the full text", submitted)
                self.assertNotIn("MIDDLE-OMITTED-" * 20, submitted)
            finally:
                session.close()

    def test_terminal_session_applies_task_metadata_timeout_before_submit(self):
        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch.dict(os.environ, {"HARNESS_TERMINAL_TASK_NAME": "overfull-hbox"}),
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="terminal",
                profile_explicit=True,
            )
            try:
                session.submit("Ensure the LaTeX document compiles without overfull hbox warnings")

                self.assertEqual(session.agent.time_budget, 750.0)
                self.assertEqual(session.agent.current_task_metadata["task_name"], "overfull-hbox")
                self.assertEqual(session.agent.current_task_metadata["category"], "debugging")
                time_middleware = next(
                    mw for mw in session.agent.middlewares if isinstance(mw, TimeBudgetMiddleware)
                )
                self.assertEqual(time_middleware.budget_seconds, 750.0)
                metadata_events = [
                    event
                    for event in session.event_bus.events
                    if event.type == "task_metadata_resolved"
                ]
                self.assertEqual(metadata_events[-1].payload["task_metadata"]["task_name"], "overfull-hbox")
                timeout_events = [
                    event
                    for event in session.event_bus.events
                    if event.type == "task_timeout_resolved"
                ]
                self.assertEqual(timeout_events[-1].payload["timeout_seconds"], 750.0)
            finally:
                session.close()

    def test_repeated_large_prompts_externalize_inside_session_without_root_duplicates(self):
        prompts = [
            "Task one\n" + ("A" * 260),
            "Task two\n" + ("B" * 260),
        ]

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch.dict("os.environ", {"HARNESS_TURN_INLINE_CHAR_LIMIT": "100"}),
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                for prompt in prompts:
                    session.submit(prompt)

                input_files = sorted((session.session.root / "inputs").glob("turn-*-prompt*.txt"))
                self.assertEqual([path.name for path in input_files], ["turn-0001-prompt.txt", "turn-0002-prompt.txt"])
                self.assertEqual([path.read_text(encoding="utf-8") for path in input_files], prompts)
                self.assertEqual(list(Path(self.temp_dir).glob("turn-*-prompt*.txt")), [])
            finally:
                session.close()

    def test_memory_middleware_dream_check_is_throttled(self):
        from harness_code_agent.runtime.middleware.memory import MemoryMiddleware

        middleware = MemoryMiddleware(workspace=self.temp_dir)
        try:
            store = object()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "HARNESS_MEMORY_DISABLED": "",
                        "HARNESS_MEMORY_DREAM_CHECK_INTERVAL_SECONDS": "60",
                    },
                ),
                patch.object(middleware, "_store", return_value=store),
                patch("harness_code_agent.memory.dream.should_dream", return_value=False) as should_dream,
            ):
                middleware.maybe_run_dream()
                middleware.maybe_run_dream()

            self.assertEqual(should_dream.call_count, 1)
        finally:
            pass

    def test_first_task_routes_inside_existing_general_session(self):
        general_conversation = FakeConversation()
        with (
            patch(
                "harness_code_agent.agent.conversation.Agent.start_conversation",
                return_value=general_conversation,
            ),
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["profile"], "general")
                self.assertEqual(metadata["initial_profile"], "general")

                result = session.submit("先给我一个实现方案，不要改代码")

                self.assertEqual(result.text, "assistant done")
                self.assertTrue(session.is_bound)
                self.assertEqual(session.profile.name(), "plan")
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["profile"], "plan")
                self.assertEqual(metadata["initial_profile"], "general")
                self.assertEqual(metadata["profile_source"], "auto route")
                self.assertEqual(general_conversation.submissions[-1], "Task:\n先给我一个实现方案，不要改代码")
                self.assertIs(session.conversation, general_conversation)
                route_events = [event for event in session.event_bus.events if event.type == "profile_route_decision"]
                self.assertTrue(route_events)
                self.assertEqual(route_events[-1].payload["source"], "local")
                self.assertTrue(route_events[-1].payload["switched"])
            finally:
                session.close()

    def test_model_route_receives_previous_turn_context_and_emits_observability(self):
        from harness_code_agent.profiles.router import RouteDecision

        conversation = FakeConversation()
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                session.last_user_task = "previous request"
                session.last_assistant_text = "previous answer"
                model_decision = RouteDecision(
                    profile_name="review",
                    confidence=0.93,
                    reason="Read-only assessment requested.",
                    source="llm",
                    action="switch_profile",
                    matched_profile="review",
                    decisive_signal="llm",
                    local_candidate="general",
                    local_confidence=0.42,
                    local_margin=0.03,
                    llm_called=True,
                    llm_confidence=0.93,
                    llm_provider="test-provider",
                    llm_model="test-fast",
                )
                with patch(
                    "harness_code_agent.core.interactive.route_profile_for_turn",
                    return_value=model_decision,
                ) as route:
                    session.submit("ambiguous follow-up")

                self.assertEqual(route.call_args.kwargs["previous_user_task"], "previous request")
                self.assertEqual(route.call_args.kwargs["previous_assistant_text"], "previous answer")
                self.assertEqual(session.profile.name(), "review")
                event = [
                    item for item in session.event_bus.events
                    if item.type == "profile_route_decision"
                ][-1]
                self.assertEqual(event.payload["local_candidate"], "general")
                self.assertEqual(event.payload["llm_provider"], "test-provider")
                self.assertEqual(event.payload["llm_model"], "test-fast")
                self.assertTrue(event.payload["llm_called"])
                self.assertEqual(event.payload["failure_type"], "")
            finally:
                session.close()

    def test_specialized_profile_direct_answer_stays_in_current_slot(self):
        conversation = FakeConversation()
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ) as start_conversation:
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                result = session.submit("你是谁")

                self.assertEqual(result.text, "assistant done")
                self.assertEqual(session.profile.name(), "coding-agent")
                self.assertEqual(session.display_profile, "coding-agent")
                self.assertEqual(start_conversation.call_count, 1)
                self.assertIn("coding-agent", session.profile_runtimes)
                self.assertNotIn("general", session.profile_runtimes)
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["profile"], "coding-agent")

                submitted = conversation.submissions[-1]
                self.assertEqual(submitted, "Task:\n你是谁")
                route_events = [event for event in session.event_bus.events if event.type == "profile_route_decision"]
                self.assertFalse(route_events)
            finally:
                session.close()

    def test_profile_slash_command_switches_existing_session(self):
        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                self.assertTrue(session.is_bound)
                self.assertEqual(session.profile.name(), "general")
                self.assertTrue(session.handle_slash_command("/profile"))
                self.assertEqual(session.last_command_result.action, "profile")
                session.switch_profile("plan")

                self.assertEqual(session.profile.name(), "plan")
                self.assertEqual(session.display_profile, "plan")
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["profile"], "plan")
                self.assertEqual(metadata["initial_profile"], "general")
                self.assertEqual(metadata["profile_source"], "slash command")
            finally:
                session.close()

    def test_selected_session_loads_context_into_existing_session(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        previous = store.create(
            profile="plan",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        store.event_bus(previous).emit("user_input", agent="main_agent", payload={"text": "previous task"})
        conversation = FakeConversation()

        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=conversation):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                session.resume_from_session(previous.id)

                self.assertTrue(session.is_bound)
                self.assertEqual(session.resume_session_id, previous.id)

                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["resumed_from"], previous.id)
                self.assertIn("Resume context:", conversation.messages[1]["content"])
                self.assertIn(previous.id, conversation.messages[1]["content"])
                self.assertIn("previous task", conversation.messages[1]["content"])
            finally:
                session.close()

    def test_profile_switch_reuses_one_shared_conversation(self):
        conversation = FakeConversation()
        conversation.response_text = "coding output"

        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                session.submit("fix the bug")
                self.assertIs(session.conversation, conversation)

                session.switch_profile("plan")
                self.assertIs(session.conversation, conversation)
                self.assertFalse(conversation.closed)

                session.switch_profile("coding-agent")
                self.assertIs(session.conversation, conversation)
                self.assertEqual(conversation.submissions, ["Task:\nfix the bug"])
                self.assertEqual(len(session.profile_runtimes), 2)
            finally:
                session.close()

    def test_initial_task_path_submits_to_live_conversation(self):
        session = self._session()
        try:
            result = session.submit("fix the tests")
            self.assertEqual(result.text, "assistant done")
            self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
            self.assertIn("fix the tests", FakeConversation.instances[0].submissions[0])
        finally:
            session.close()

    def test_turn_summary_emits_only_for_tui_event_listener(self):
        class ToolingConversation(FakeConversation):
            def submit(self, task, cancellation_token=None):
                self.submissions.append(task)
                self._event_bus.emit_event(ToolResultEvent(tool="read_file", status="success").to_event())
                self._event_bus.emit_event(ToolResultEvent(tool="read_file", status="success").to_event())
                self._event_bus.emit_event(ToolResultEvent(tool="read_file", status="success").to_event())
                return self.response_text

        def fake_summary(*args, **kwargs):
            return SimpleNamespace(
                summary="- summarized",
                tool_counts={"read_file": 3},
                changed_files=[],
                generated_by={"intensity": "fast", "model": "custom-fast"},
            )

        events = []
        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=ToolingConversation()),
            patch("harness_code_agent.core.interactive.generate_turn_summary", side_effect=fake_summary),
        ):
            session = InteractiveSession(cwd=self.temp_dir, event_listener=events.append)
            try:
                session.submit("fix with tools")
                event_types = [event.type for event in session.event_bus.events]
                self.assertIn("turn_summary", event_types)
                turn_summary = next(event for event in session.event_bus.events if event.type == "turn_summary")
                self.assertEqual(turn_summary.payload["generated_by"]["intensity"], "fast")
            finally:
                session.close()

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=ToolingConversation()),
            patch("harness_code_agent.core.interactive.generate_turn_summary", side_effect=fake_summary),
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                session.submit("fix with tools")
                event_types = [event.type for event in session.event_bus.events]
                self.assertNotIn("turn_summary", event_types)
            finally:
                session.close()

    def test_cancelled_submit_does_not_emit_assistant_message_event(self):
        from harness_code_agent.agent.cancellation import CancelledError

        class CancellingConversation(FakeConversation):
            def submit(self, task, cancellation_token=None):
                self.submissions.append(task)
                if cancellation_token is not None:
                    cancellation_token.cancel()
                raise CancelledError("Turn cancelled by user")

        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=CancellingConversation()):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                with self.assertRaises(CancelledError):
                    session.submit("cancel this")

                event_types = [event.type for event in session.event_bus.events]
                self.assertIn("user_input", event_types)
                self.assertIn("turn_started", event_types)
                self.assertNotIn("assistant_message", event_types)
            finally:
                session.close()

    def test_interrupt_current_shell_delegates_to_active_shell_session(self):
        session = self._session()
        try:
            interrupted = []
            shell = types.SimpleNamespace(interrupt=lambda: interrupted.append(True))
            session.ensure_profile_bound_for_first_task("noop")
            session.conversation.runtime_state = types.SimpleNamespace(shell_session=shell)

            self.assertTrue(session.interrupt_current_shell())
            self.assertEqual(interrupted, [True])
        finally:
            session.close()

    def test_interactive_session_builds_profile_tools_from_allowed_permissions(self):
        class FakePermissionProfile:
            def name(self):
                return "fake-permissions"

            def main_agent(self):
                return AgentConfig(
                    system_prompt="fake",
                    allowed_tool_permissions={"read"},
                    allowed_tool_names={"web_search"},
                    blocked_tool_names={"ask_user"},
                )

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakePermissionProfile()),
            patch("harness_code_agent.agent.conversation.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-permissions")
            try:
                session.ensure_profile_bound_for_first_task("inspect")
                tool_names = {
                    schema["function"]["name"]
                    for schema in RecordingInteractiveAgent.init_kwargs["tool_schemas"]
                }

                self.assertIn("read_file", tool_names)
                self.assertIn("web_search", tool_names)
                self.assertNotIn("ask_user", tool_names)
                self.assertNotIn("write_file", tool_names)
                self.assertNotIn("run_bash", tool_names)
            finally:
                session.close()

    def test_profile_can_disable_memory_middleware(self):
        from harness_code_agent.runtime.middleware import MemoryMiddleware

        class FakeNoMemoryProfile:
            def name(self):
                return "fake-no-memory"

            def main_agent(self):
                return AgentConfig(system_prompt="fake", memory_enabled=False)

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakeNoMemoryProfile()),
            patch("harness_code_agent.agent.conversation.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-no-memory")
            try:
                session.ensure_profile_bound_for_first_task("inspect")

                self.assertFalse(
                    any(
                        isinstance(middleware, MemoryMiddleware)
                        for middleware in RecordingInteractiveAgent.init_kwargs["middlewares"]
                    )
                )
            finally:
                session.close()

    def test_interactive_session_registers_mcp_tools_from_session_registry(self):
        from harness_code_agent.runtime import tools

        class FakeMcpManager:
            def __init__(self):
                self.closed = False

            @classmethod
            def from_workspace(cls, workspace):
                return cls()

            def connect_all(self):
                return None

            def register_tools(self, registry):
                registry.register(
                    {
                        "type": "function",
                        "function": {
                            "name": "mcp__docs__search",
                            "description": "Search docs",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    lambda **_: "ok",
                    permission="read",
                )

            def close(self):
                self.closed = True

        class FakeToolProfile:
            def name(self):
                return "fake-tools"

            def main_agent(self):
                return AgentConfig(system_prompt="fake", allowed_tool_permissions={"read"})

            def acceptance_criteria(self):
                return []

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakeToolProfile()),
            patch("harness_code_agent.core.interactive.McpClientManager", FakeMcpManager),
            patch("harness_code_agent.agent.conversation.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
            try:
                session.ensure_profile_bound_for_first_task("inspect docs")
                tool_names = {
                    schema["function"]["name"]
                    for schema in session.agent.tool_schemas
                }

                self.assertIn("read_file", tool_names)
                self.assertNotIn("mcp__docs__search", tool_names)

                session.warm_mcp_tools()
                tool_names = {
                    schema["function"]["name"]
                    for schema in session.agent.tool_schemas
                }
                self.assertIn("mcp__docs__search", tool_names)
                self.assertIsNot(session.tool_registry, tools.BUILTIN_TOOL_REGISTRY)
                self.assertIs(session.tool_context.tool_registry, session.tool_registry)
            finally:
                session.close()

    def test_mcp_slash_command_opens_management_panel(self):
        class FakeMcpManager:
            instances: ClassVar[list] = []

            def __init__(self):
                self.closed = False
                self.registered = False
                self.__class__.instances.append(self)

            @classmethod
            def from_workspace(cls, workspace):
                return cls()

            def connect_all(self):
                return None

            def register_tools(self, registry):
                self.registered = True

            def status_report(self):
                return "MCP status\nserver docs: connected"

            def tools_report(self):
                return "MCP tools\nmcp__docs__search"

            def close(self):
                self.closed = True

        with (
            patch("harness_code_agent.core.interactive.McpClientManager", FakeMcpManager),
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                out = StringIO()
                session.output_sink = lambda text: print(text, file=out)

                self.assertTrue(session.handle_slash_command("/mcp"))
                self.assertEqual(session.last_command_result.action, "mcp")
                self.assertEqual(out.getvalue(), "")
            finally:
                session.close()

    def test_interactive_session_injects_workspace_harness_md_into_system_prompt(self):
        Path(self.temp_dir, "HARNESS.md").write_text(
            "# Project Rules\n\nAlways prefer focused tests.\n",
            encoding="utf-8",
        )

        class FakeToolProfile:
            def name(self):
                return "fake-tools"

            def main_agent(self):
                return AgentConfig(system_prompt="fake")

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakeToolProfile()),
            patch("harness_code_agent.agent.conversation.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
            try:
                session.ensure_profile_bound_for_first_task("inspect")
                prompt = RecordingInteractiveAgent.init_args[1]
                self.assertIn("## HARNESS.md", prompt)
                self.assertIn("Always prefer focused tests.", prompt)
                self.assertIn("## Profile Acceptance Criteria", prompt)
                self.assertIn("## Agent Identity and Judgment", prompt)
                self.assertIn("## Profile Contract", prompt)
                self.assertEqual(session.format_task("hello"), "Task:\nhello")
            finally:
                session.close()

    def test_interactive_session_missing_harness_md_does_not_change_prompt(self):
        class FakeToolProfile:
            def name(self):
                return "fake-tools"

            def main_agent(self):
                return AgentConfig(system_prompt="fake")

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakeToolProfile()),
            patch("harness_code_agent.agent.conversation.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
            try:
                session.ensure_profile_bound_for_first_task("inspect")
                prompt = RecordingInteractiveAgent.init_args[1]
                self.assertNotIn("## HARNESS.md", prompt)
            finally:
                session.close()

    def test_plan_profile_captures_markdown_and_offers_handoff_choice(self):
        FakeConversation.response_text = "# Title\n\n## Summary\n\nPlan body"
        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="plan")
            try:
                result = session.submit("plan the parser fix")

                self.assertEqual(session.pending_plan_markdown, "# Title\n\n## Summary\n\nPlan body")
                self.assertIn("执行计划", result.notice)
                self.assertIn("修改计划", result.notice)
                self.assertEqual(result.checkpoint, "checkpoint auto off")
                plan_path = Path(self.temp_dir, "global_plan", "current", "plan.md")
                self.assertEqual(plan_path.read_text(encoding="utf-8"), "# Title\n\n## Summary\n\nPlan body\n")
                self.assertFalse(Path(self.temp_dir, ".harness", "sessions", session.session.id, "planning", "state.json").exists())
                self.assertFalse(Path(self.temp_dir, "global_plan", "current", "status.md").exists())
                self.assertFalse(Path(self.temp_dir, "global_plan", "current", "final.md").exists())
            finally:
                session.close()

    def test_plan_continue_switches_to_coding_agent_and_injects_markdown(self):
        conversation = FakeConversation()
        conversation.response_text = "# Title\n\n## Summary\n\nPlan body"

        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="plan")
            try:
                session.submit("plan the parser fix")
                conversation.response_text = "implemented"
                result = session.submit("继续")

                self.assertEqual(result.text, "implemented")
                self.assertEqual(session.profile.name(), "coding-agent")
                self.assertIsNone(session.pending_plan_markdown)
                self.assertEqual(session.pending_plan_revision, 0)
                self.assertEqual(len(conversation.submissions), 2)
                task = conversation.submissions[-1]
                self.assertIn("Execute the approved implementation plan", task)
                self.assertIn("Approved plan:\n# Title\n\n## Summary\n\nPlan body", task)
            finally:
                session.close()

    def test_plan_feedback_keeps_plan_mode_and_updates_pending_markdown(self):
        plan_conversation = FakeConversation()
        plan_conversation.response_text = "# Title\n\n## Summary\n\nPlan v1"

        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=plan_conversation):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="plan")
            try:
                session.submit("plan the parser fix")
                plan_conversation.response_text = "# Title\n\n## Summary\n\nPlan v2"
                result = session.submit("add migration risk")

                self.assertEqual(result.text, "# Title\n\n## Summary\n\nPlan v2")
                self.assertEqual(session.profile.name(), "plan")
                self.assertEqual(session.pending_plan_markdown, "# Title\n\n## Summary\n\nPlan v2")
                self.assertEqual(session.pending_plan_revision, 2)
                plan_path = Path(self.temp_dir, "global_plan", "current", "plan.md")
                self.assertEqual(plan_path.read_text(encoding="utf-8"), "# Title\n\n## Summary\n\nPlan v2\n")
                self.assertIn("User feedback:\nadd migration risk", plan_conversation.submissions[-1])
            finally:
                session.close()

    def test_profile_command_opens_picker_and_direct_selection_pins_mode(self):
        conversation = FakeConversation()
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                self.assertTrue(session.handle_slash_command("/profile"))
                self.assertEqual(session.last_command_result.action, "profile")
                session.switch_profile("plan")
                self.assertEqual(session.profile.name(), "plan")
                self.assertEqual(session.display_routing_mode, "pinned")
            finally:
                session.close()

    def test_fork_current_session_keeps_live_conversation_and_enters_branch(self):
        conversation = FakeConversation()
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                session.submit("inspect the parser")
                source_id = session.session.id
                result = session.fork_current_session()

                self.assertIn("已进入会话分支", result)
                self.assertNotEqual(session.session.id, source_id)
                self.assertIs(session.conversation, conversation)
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["forked_from"], source_id)
                self.assertEqual(metadata["profile"], session.profile.name())
            finally:
                session.close()

    def test_profile_switch_keeps_full_message_history(self):
        conversation = FakeConversation()
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            return_value=conversation,
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                session.submit("inspect the auth bug")

                session.switch_profile("plan")

                self.assertEqual(session.profile.name(), "plan")
                self.assertIs(session.conversation, conversation)
                self.assertGreaterEqual(len(conversation.messages), 3)
                self.assertIn("inspect the auth bug", conversation.messages[1]["content"])
                self.assertEqual(session.profile_history[-1].previous, "coding-agent")
                self.assertEqual(session.profile_history[-1].current, "plan")
            finally:
                session.close()

    def test_switching_profile_clears_pending_plan(self):
        FakeConversation.response_text = "# Title\n\n## Summary\n\nPlan body"
        conversations = [FakeConversation(), FakeConversation()]
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            side_effect=conversations,
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="plan")
            try:
                session.submit("plan the parser fix")
                self.assertIsNotNone(session.pending_plan_markdown)

                session.switch_profile("coding-agent")

                self.assertEqual(session.profile.name(), "coding-agent")
                self.assertIsNone(session.pending_plan_markdown)
                self.assertEqual(session.pending_plan_revision, 0)
            finally:
                session.close()

    def test_file_mention_injects_light_reference_not_content(self):
        Path(self.temp_dir, "README.md").write_text("hello docs\n", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        resolved = resolve_mentions(
            "use @file:README.md please",
            workspace_root=self.temp_dir,
            session_store=store,
        )
        formatted = render_mention_context(resolved)

        self.assertIn("Mention context:", formatted)
        self.assertIn("resolved as file", formatted)
        self.assertIn("README.md", formatted)
        self.assertIn("Use read_file to inspect this file if needed.", formatted)
        self.assertNotIn("hello docs", formatted)

    def test_bare_at_file_text_is_not_resolved_as_mention(self):
        Path(self.temp_dir, "README.md").write_text("hello docs\n", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        resolved = resolve_mentions(
            "use @README.md please",
            workspace_root=self.temp_dir,
            session_store=store,
        )

        self.assertEqual(resolved, [])

    def test_directory_mention_is_light_reference(self):
        Path(self.temp_dir, "docs").mkdir()
        store = SessionStore(Path(self.temp_dir) / ".harness")

        resolved = resolve_mentions(
            "inspect @file:docs",
            workspace_root=self.temp_dir,
            session_store=store,
        )
        formatted = render_mention_context(resolved)

        self.assertEqual(resolved[0].kind, "directory")
        self.assertIn("resolved as directory", formatted)
        self.assertIn("Use list_files to inspect this directory if needed.", formatted)

    def test_skill_mention_is_not_a_supported_mention(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")

        resolved = resolve_mentions(
            "use @skill:diagnose",
            workspace_root=self.temp_dir,
            session_store=store,
        )

        self.assertEqual(resolved, [])

    def test_user_skill_command_becomes_an_agent_turn(self):
        skills_dir = Path(self.temp_dir) / "skills"
        skill_dir = skills_dir / "triage"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: triage\n"
            "description: Triage an issue.\n"
            "disable-model-invocation: true\n"
            "---\n\n"
            "Inspect the issue before deciding.\n",
            encoding="utf-8",
        )
        session = InteractiveSession.__new__(InteractiveSession)
        session.skill_registry = SkillRegistry(skills_dir)
        session.ensure_profile_bound_for_first_task = Mock()
        expected = SimpleNamespace(text="done")
        session._submit_to_current_agent = Mock(return_value=expected)

        result = session.submit("/triage 42")

        self.assertIs(result, expected)
        session._submit_to_current_agent.assert_called_once()
        call = session._submit_to_current_agent.call_args
        self.assertEqual(call.args[0], "/triage 42")
        self.assertIn("Inspect the issue before deciding.", call.kwargs["turn_instruction"])
        self.assertIn("Arguments: 42", call.kwargs["turn_instruction"])

    def test_missing_file_mention_fails_fast(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")

        with self.assertRaises(MentionResolutionError):
            resolve_mentions(
                "read @file:missing.md",
                workspace_root=self.temp_dir,
                session_store=store,
            )

    def test_mentions_can_be_turned_off_for_benchmark_prompts(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")

        with patch.dict(os.environ, {"HARNESS_MENTION_MODE": "off"}):
            resolved = resolve_mentions(
                "Java annotation @com.google.inject.name.Named(value=ForTheEagerness should stay literal",
                workspace_root=self.temp_dir,
                session_store=store,
            )

        self.assertEqual(resolved, [])

    def test_file_mention_rejects_path_escape(self):
        outside = Path(self.temp_dir).parent / "outside-mention.txt"
        outside.write_text("nope", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        try:
            with self.assertRaises(MentionResolutionError):
                resolve_mentions(
                    "read @file:../outside-mention.txt",
                    workspace_root=self.temp_dir,
                    session_store=store,
                )
        finally:
            outside.unlink(missing_ok=True)

    def test_session_mention_is_injected(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        store.event_bus(session).emit("session_started", agent="main_agent", payload={"task": "fix"})

        resolved = resolve_mentions(
            f"continue @session:{session.id}",
            workspace_root=self.temp_dir,
            session_store=store,
        )

        self.assertEqual(resolved[0].kind, "session")
        self.assertIn(session.id, resolved[0].content)
        self.assertIn("Session summary", resolved[0].content)
        self.assertIn("profile: coding-agent", resolved[0].content)
        self.assertIn("recent_events:", resolved[0].content)
        self.assertIn("session_started", resolved[0].content)

    def test_print_session_uses_human_readable_summary(self):
        from harness_code_agent.core.formatters import print_session

        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        bus = store.event_bus(session)
        bus.emit("turn_started", agent="main_agent", payload={"turn": 1})
        bus.emit_event(ToolResultEvent(tool="read_file", status="success", output="ok").to_event())
        bus.emit_event(FileChangeEvent(path="app.py").to_event())

        output = StringIO()
        with redirect_stdout(output):
            print_session(store, session.id)

        text = output.getvalue()
        self.assertIn("Session summary", text)
        self.assertIn(f"id: {session.id}", text)
        self.assertIn("tools: 1 call(s): read_file=1", text)
        self.assertIn("changed_files: app.py", text)

    def test_interactive_close_writes_session_summary(self):
        session = self._session()
        try:
            session.submit("summarize this session")
            session_id = session.session.id
        finally:
            session.close()

        summary_path = Path(self.temp_dir) / ".harness" / "sessions" / session_id / "summary.md"
        text = summary_path.read_text(encoding="utf-8")
        metadata = session.session_store.read_metadata(session_id)
        listed = next(
            item for item in session.session_store.list_sessions()
            if item.get("id") == session_id
        )
        self.assertIn("Session summary", text)
        self.assertIn(f"id: {session_id}", text)
        self.assertIn("status: closed", text)
        self.assertEqual(metadata["status"], "closed")
        self.assertEqual(listed["status"], "closed")
        self.assertIn("final_report: closed - assistant done", text)
        self.assertIn("task_outcome: unknown", text)

        events = session.session_store.read_events(session_id)
        event_types = [event["type"] for event in events]
        final_report = next(event for event in events if event["type"] == "final_report")
        self.assertEqual(final_report["payload"]["session_id"], session_id)
        self.assertEqual(final_report["payload"]["status"], "closed")
        self.assertEqual(final_report["payload"]["reason"], "user_exit")
        self.assertEqual(final_report["payload"]["summary"], "assistant done")
        self.assertEqual(final_report["payload"]["statistics"]["user_inputs"], 1)
        self.assertEqual(final_report["payload"]["statistics"]["assistant_messages"], 1)
        self.assertEqual(events[-1]["type"], "session_finished")
        self.assertEqual(events[-1]["payload"]["reason"], "user_exit")
        self.assertEqual(events[-1]["payload"]["status"], "closed")
        self.assertNotIn("task_outcome", event_types)

    def test_interactive_close_finishes_session_when_final_report_event_read_fails(self):
        session = self._session()
        session.ensure_profile_bound_for_first_task("noop")
        session_id = session.session.id
        original_read_events = session.session_store.read_events
        calls = 0

        def flaky_read_events(read_session_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("events unreadable")
            return original_read_events(read_session_id)

        with patch.object(session.session_store, "read_events", side_effect=flaky_read_events):
            session.close()

        metadata = session.session_store.read_metadata(session_id)
        events = session.session_store.read_events(session_id)

        self.assertEqual(metadata["status"], "closed")
        self.assertEqual(events[-1]["type"], "session_finished")
        self.assertEqual(events[-1]["payload"]["reason"], "user_exit")
        self.assertNotIn("final_report", [event["type"] for event in events])

    def test_interactive_close_check_and_set_is_thread_safe(self):
        class SlowFalse:
            def __bool__(self):
                time.sleep(0.05)
                return False

        class CountingConversation(FakeConversation):
            close_count = 0
            close_lock = threading.Lock()

            def close(self):
                with self.close_lock:
                    type(self).close_count += 1
                super().close()

        conversation = CountingConversation()
        barrier = threading.Barrier(2)
        errors = []

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=conversation),
            patch("harness_code_agent.core.interactive.stop_dev_server") as stop_dev_server,
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            session.submit("noop")
            session._closed = SlowFalse()

            def close_session():
                try:
                    barrier.wait()
                    session.close()
                except Exception as exc:  # noqa: BLE001 - the test collects whatever races raise
                    errors.append(exc)

            threads = [threading.Thread(target=close_session) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(CountingConversation.close_count, 1)
        self.assertEqual(stop_dev_server.call_count, 1)

    def test_hca_session_show_latest_prints_latest_summary_without_api_key(self):
        from harness_code_agent import cli

        config.API_KEY = ""
        store = SessionStore(Path(self.temp_dir) / ".harness")
        older = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        latest = store.create(
            profile="plan",
            cwd=self.temp_dir,
            model="model-b",
            permission_mode="read-only",
        )
        store.event_bus(older).emit("user_input", agent="main_agent", payload={"text": "old"})
        store.event_bus(latest).emit("user_input", agent="main_agent", payload={"text": "new"})
        store.write_summary(latest.id)

        output = StringIO()
        with redirect_stdout(output):
            result = cli.main(["session", "show", "latest"])

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Session summary", text)
        self.assertIn(f"id: {latest.id}", text)
        self.assertNotIn(f"id: {older.id}", text)

    def test_hca_session_observe_latest_prints_dashboard_without_api_key(self):
        from harness_code_agent import cli

        config.API_KEY = ""
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        store.event_bus(session).emit(
            "llm_usage",
            agent="main_agent",
            payload={"prompt_tokens": 100, "cached_tokens": 90, "total_tokens": 120},
        )

        output = StringIO()
        with redirect_stdout(output):
            result = cli.main(["session", "observe", "latest"])

        self.assertEqual(result, 0)
        self.assertIn("Observability dashboard", output.getvalue())
        self.assertIn("cache hit ratio: 90.0%", output.getvalue())

    def test_hca_session_observe_project_export_writes_audit_artifacts(self):
        from harness_code_agent import cli

        config.API_KEY = ""
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        store.event_bus(session).emit(
            "llm_usage",
            agent="main_agent",
            payload={"prompt_tokens": 100, "cached_tokens": 50, "total_tokens": 120},
        )

        output = StringIO()
        with redirect_stdout(output):
            result = cli.main(["session", "observe", "project", "--export"])

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("observability_export_markdown:", text)
        self.assertIn("observability_export_json:", text)
        self.assertTrue((Path(self.temp_dir) / ".harness" / "reports" / "observability").exists())

    def test_clean_checkpoint_is_skipped(self):
        session = self._session()
        try:
            session.ensure_profile_bound_for_first_task("noop")
            self.assertEqual(session.create_checkpoint(manual=True), "no changes to checkpoint")
        finally:
            session.close()

    def test_git_init_failure_raises_by_default(self):
        from harness_code_agent.core.git_helpers import _ensure_git_repository

        with (
            patch("harness_code_agent.core.git_helpers.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["git", "init"])),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            _ensure_git_repository(Path(self.temp_dir))

        self.assertFalse((Path(self.temp_dir) / ".git").exists())

    def test_terminal_session_can_disable_checkpoint_when_git_init_fails(self):
        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.core.interactive._ensure_git_repository", side_effect=subprocess.CalledProcessError(1, ["git", "init"])),
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="terminal",
                profile_explicit=True,
                allow_checkpoint_init_failure=True,
            )
            try:
                session.ensure_profile_bound_for_first_task("noop")
                self.assertEqual(
                    session.create_checkpoint(manual=True),
                    "checkpoint skipped: git repository unavailable",
                )

                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["checkpoint_status"], "disabled")
                self.assertIn("CalledProcessError", metadata["checkpoint_init_error"])
                events = session.session_store.read_events(session.session.id)
                self.assertTrue(any(event["type"] == "checkpoint_disabled" for event in events))
            finally:
                session.close()

    def test_terminal_slash_command_is_not_product_visible(self):
        session = InteractiveSession(cwd=self.temp_dir)
        output = StringIO()
        session.output_sink = lambda text: print(text, file=output)
        try:
            self.assertTrue(session.handle_slash_command("/terminal"))
            self.assertEqual(session.profile.name(), "general")
            self.assertIn("未知的斜杠命令：/terminal", output.getvalue())
        finally:
            session.close()

    def test_removed_profiles_slash_command_is_not_available(self):
        session = InteractiveSession(cwd=self.temp_dir)
        output = StringIO()
        session.output_sink = lambda text: print(text, file=output)
        try:
            self.assertTrue(session.handle_slash_command("/profiles"))
            text = output.getvalue()
            self.assertIn("未知的斜杠命令", text)
        finally:
            session.close()

    def test_handle_checkpoint_every_cadence(self):
        session = self._session()
        try:
            res = session._handle_checkpoint_command(["every", "3", "turns"])
            self.assertEqual(session.checkpoint.every_turns, 3)
            self.assertIn("every 3 turns", res)

            res = session._handle_checkpoint_command(["every", "5"])
            self.assertEqual(session.checkpoint.every_turns, 5)
            self.assertIn("every 5 turns", res)

            res = session._handle_checkpoint_command(["every", "1", "turn"])
            self.assertEqual(session.checkpoint.every_turns, 1)
            self.assertIn("every 1 turns", res)

            with self.assertRaises(ValueError):
                session._handle_checkpoint_command(["every", "abc"])

            with self.assertRaises(ValueError):
                session._handle_checkpoint_command(["every", "-1"])
        finally:
            session.close()


    def test_dirty_checkpoint_commits_changes(self):
        session = self._session()
        try:
            session.ensure_profile_bound_for_first_task("noop")
            Path(self.temp_dir, "app.py").write_text("print('hi')\n", encoding="utf-8")

            result = session.create_checkpoint(manual=True)

            self.assertIn("checkpoint created:", result)
            self.assertEqual(self._git("status", "--porcelain", "--", "app.py"), "")
            self.assertTrue(self._git("log", "--format=%s", "-1").startswith("checkpoint: "))
        finally:
            session.close()

    def test_auto_checkpoint_does_not_commit_preexisting_dirty_files(self):
        self._ensure_git()
        Path(self.temp_dir, "preexisting.txt").write_text("old\n", encoding="utf-8")
        baseline = git_dirty_paths(Path(self.temp_dir))
        session = self._session()
        try:
            session.ensure_profile_bound_for_first_task("noop")
            Path(self.temp_dir, "new.txt").write_text("new\n", encoding="utf-8")

            result = session.create_checkpoint(manual=False, baseline_dirty=baseline)

            self.assertIn("checkpoint created:", result)
            self.assertIn("?? preexisting.txt", self._git("status", "--porcelain", "--", "preexisting.txt"))
            self.assertEqual(self._git("status", "--porcelain", "--", "new.txt"), "")
        finally:
            session.close()

    def test_git_dirty_paths_filters_verify_cache_paths(self):
        self._ensure_git()
        Path(self.temp_dir, "app.py").write_text("print('hi')\n", encoding="utf-8")
        Path(self.temp_dir, ".pytest_cache", "v", "cache").mkdir(parents=True)
        Path(self.temp_dir, ".pytest_cache", "v", "cache", "nodeids").write_text("[]\n", encoding="utf-8")
        Path(self.temp_dir, "__pycache__").mkdir()
        Path(self.temp_dir, "__pycache__", "app.cpython-311.pyc").write_bytes(b"cache")

        dirty = git_dirty_paths(Path(self.temp_dir))

        self.assertIn("app.py", dirty)
        self.assertFalse(any(".pytest_cache" in path for path in dirty))
        self.assertFalse(any("__pycache__" in path for path in dirty))

    def test_git_baseline_timeout_is_bounded_and_reported_as_unavailable(self):
        self._ensure_git()
        with patch(
            "harness_code_agent.core.git_helpers.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "status"], 0.5),
        ) as run:
            baseline = capture_git_baseline(Path(self.temp_dir))

        self.assertIsNone(baseline)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_unavailable_git_baseline_skips_auto_checkpoint(self):
        session = self._session()
        try:
            session.ensure_profile_bound_for_first_task("noop")
            session.checkpoint.auto = True
            with patch.object(session, "create_checkpoint") as create_checkpoint:
                result = session._maybe_auto_checkpoint(baseline=None)

            self.assertEqual(result, "checkpoint skipped: git status unavailable")
            create_checkpoint.assert_not_called()
        finally:
            session.close()

    def test_auto_checkpoint_is_disabled_by_default(self):
        session = self._session()
        try:
            self.assertFalse(session.checkpoint.auto)
            self.assertFalse(Path(self.temp_dir, ".git").exists())
            with patch("harness_code_agent.core.interactive.capture_git_baseline") as capture:
                result = session.submit("hello")

            capture.assert_not_called()
            self.assertEqual(result.checkpoint, "checkpoint auto off")
        finally:
            session.close()

    def test_hca_tui_startup_passes_task_and_profile(self):
        from harness_code_agent import cli

        class TtyBuffer(StringIO):
            def isatty(self):
                return True

        cases = [
            (["veriforge", "fix", "tests"], {"first_task": "fix tests"}),
            (["veriforge", "--profile", "terminal", "fix", "shell"], {"profile_name": "terminal", "first_task": "fix shell"}),
            (["veriforge", "--theme", "light", "--icons", "nerd"], {"theme": "light", "icons": "nerd"}),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                with (
                    patch("harness_code_agent.cli.TuiApp") as tui_app,
                    patch.object(sys, "stdin", TtyBuffer()),
                    patch.object(sys, "stdout", TtyBuffer()),
                    patch.object(sys, "argv", argv),
                ):
                    tui_app.return_value.run.return_value = 0
                    result = cli.main()

                self.assertEqual(result, 0)
                tui_app.assert_called_once()
                for key, value in expected.items():
                    self.assertEqual(tui_app.call_args.kwargs[key], value)

    def test_hca_tui_startup_disables_console_logging(self):
        from harness_code_agent import cli

        class TtyBuffer(StringIO):
            def isatty(self):
                return True

        with (
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch("harness_code_agent.core.logging_config.setup_logging") as setup_logging,
            patch.object(sys, "stdin", TtyBuffer()),
            patch.object(sys, "stdout", TtyBuffer()),
        ):
            tui_app.return_value.run.return_value = 0
            result = cli.main(["tiny", "demo"])

        self.assertEqual(result, 0)
        setup_logging.assert_called_once_with(verbose=False, console=False)

    def test_hca_print_mode_flags_submit_without_tui(self):
        from harness_code_agent import cli

        for flag in ("-p", "--print"):
            with self.subTest(flag=flag):
                FakeConversation.instances = []
                with (
                    patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
                    patch("harness_code_agent.cli.TuiApp") as tui_app,
                    patch.object(sys, "argv", ["veriforge", flag, "fix", "tests"]),
                ):
                    result = cli.main()

                self.assertEqual(result, 0)
                self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
                self.assertIn("fix tests", FakeConversation.instances[0].submissions[0])
                tui_app.assert_not_called()

    def test_hca_print_mode_requires_task(self):
        from harness_code_agent import cli

        errors = StringIO()
        with (
            redirect_stderr(errors),
            patch.object(sys, "argv", ["veriforge", "-p"]),
        ):
            result = cli.main()

        self.assertEqual(result, 2)
        self.assertIn("no task provided", errors.getvalue())

    def test_batch_local_slash_command_is_not_sent_to_model(self):
        from harness_code_agent import cli

        calls = []
        root = Path.cwd()

        class FakeSession:
            session_id = "session-local"
            cwd = root
            skill_registry = None

            def __init__(self, **kwargs):
                self.output_sink = kwargs.get("output_sink", print)

            def handle_slash_command(self, line):
                calls.append(("slash", line))
                return True

            def submit(self, line):
                calls.append(("submit", line))
                raise AssertionError("local slash commands must not reach the model")

            def close(self):
                calls.append(("close", ""))

        with patch("harness_code_agent.cli.InteractiveSession", FakeSession):
            result = cli.run_batch(
                cwd=root,
                profile_name="coding-agent",
                first_task="/profile",
            )

        self.assertEqual(result, 0)
        self.assertIn(("slash", "/profile"), calls)
        self.assertNotIn(("submit", "/profile"), calls)

    def test_stream_callback_auto_respects_tty(self):
        from harness_code_agent import cli

        class TtyBuffer(StringIO):
            def isatty(self):
                return True

        output = TtyBuffer()
        with (
            patch("harness_code_agent.cli.config.STREAM", "auto"),
            patch.object(sys, "stdout", output),
        ):
            callback = cli._build_stream_callback()
            callback("hel")
            callback("lo")

        self.assertEqual(output.getvalue(), "hello")

        with patch("harness_code_agent.cli.config.STREAM", "auto"):
            self.assertIsNone(cli._build_stream_callback())

    def test_config_show_includes_runtime_settings(self):
        from harness_code_agent.core.formatters import format_config_show

        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch.object(config, "DOCKER_IMAGE", "python:3.12"),
            patch.object(config, "DOCKER_NETWORK", "none"),
            patch.object(config, "DOCKER_USER", "1000:1000"),
        ):
            text = format_config_show(Path(self.temp_dir))

        self.assertIn("sandbox_mode: docker", text)
        self.assertIn("docker_image: python:3.12", text)
        self.assertIn("docker_network: none", text)
        self.assertIn("docker_user: 1000:1000", text)
        self.assertIn(f"model_intensity: {config.MODEL_INTENSITY}", text)
        self.assertIn(f"model_profile_fast: {config.MODEL_PROFILES['fast'].model}", text)
        self.assertIn(f"model_profile_hard: {config.MODEL_PROFILES['hard'].model}", text)
        self.assertIn(f"max_agent_total_tokens: {config.MAX_AGENT_TOTAL_TOKENS}", text)

    def test_doctor_docker_mode_reports_daemon_status(self):
        from harness_code_agent.core.formatters import format_doctor

        cases = [
            ((True, "Docker 27.0.3"), 0, "Docker 27.0.3"),
            ((False, "Docker daemon unreachable (exit code 1)"), 1, "FAIL"),
        ]
        for daemon_result, minimum_failures, expected in cases:
            with self.subTest(daemon_result=daemon_result):
                with (
                    patch.object(config, "SANDBOX_MODE", "docker"),
                    patch("harness_code_agent.core.formatters.docker_cli_path", return_value="/usr/bin/docker"),
                    patch("harness_code_agent.core.formatters.docker_info_check", return_value=daemon_result),
                ):
                    text, failures = format_doctor(Path(self.temp_dir))

                self.assertIn("Docker CLI", text)
                self.assertIn("Docker daemon", text)
                self.assertIn(expected, text)
                self.assertGreaterEqual(failures, minimum_failures)

    def test_print_turn_result_does_not_duplicate_streamed_text(self):
        from harness_code_agent.core.interactive import TurnResult, print_turn_result

        output = StringIO()
        with redirect_stdout(output):
            print_turn_result(TurnResult(text="already streamed", checkpoint="checkpoint", streamed=True))

        self.assertNotIn("already streamed", output.getvalue())
        self.assertIn("checkpoint", output.getvalue())

    def test_hca_interactive_requires_tty(self):
        from harness_code_agent import cli

        errors = StringIO()
        with (
            redirect_stderr(errors),
            patch.object(sys, "argv", ["veriforge"]),
            patch.object(sys.stdin, "read", return_value=""),
        ):
            result = cli.main()

        self.assertEqual(result, 2)
        self.assertIn("no task provided", errors.getvalue())

    def test_hca_no_tty_auto_degrades_to_batch(self):
        from harness_code_agent import cli

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch.object(sys, "argv", ["veriforge"]),
            patch.object(sys.stdin, "read", return_value="fix from pipe"),
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(sys.stdout, "isatty", return_value=False),
        ):
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
        self.assertIn("fix from pipe", FakeConversation.instances[0].submissions[0])
        tui_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
