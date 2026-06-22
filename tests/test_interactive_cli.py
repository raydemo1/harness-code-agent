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
from unittest.mock import patch
from unittest.mock import Mock


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent import config
from harness_code_agent.core.interactive import (
    InteractiveSession,
    PROFILE_SLASH_ALIASES,
    git_dirty_paths,
)
from harness_code_agent.core.mentions import (
    MentionResolutionError,
    format_turn_with_mentions,
    resolve_mentions,
)
from harness_code_agent.sessions.events import FileChangeEvent, ToolResultEvent
from harness_code_agent.sessions.store import SessionStore
from harness_code_agent.profiles.base import AgentConfig
from harness_code_agent.skills import SkillRegistry


class FakeConversation:
    instances = []
    response_text = "assistant done"

    def __init__(self):
        self.messages = [{"role": "system", "content": "fake"}]
        self.submissions = []
        self.closed = False
        self.response_text = self.__class__.response_text
        self.__class__.instances.append(self)

    def submit(self, task, cancellation_token=None):
        self.submissions.append(task)
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

    def start_conversation(self):
        return FakeConversation()


class InteractiveCliTests(unittest.TestCase):
    def setUp(self):
        self.old_workspace = config.WORKSPACE
        self.old_api_key = config.API_KEY
        self.old_cwd = os.getcwd()
        self.temp_dir = tempfile.mkdtemp()
        config.API_KEY = "test-key"
        FakeConversation.instances = []
        FakeConversation.response_text = "assistant done"
        self._git("init")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "init")
        os.chdir(self.temp_dir)

    def tearDown(self):
        os.chdir(self.old_cwd)
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
        plan_conversation = FakeConversation()
        with (
            patch(
                "harness_code_agent.agent.conversation.Agent.start_conversation",
                side_effect=[general_conversation, plan_conversation],
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
                self.assertEqual(plan_conversation.submissions[-1], "Task:\n先给我一个实现方案，不要改代码")
                route_events = [event for event in session.event_bus.events if event.type == "profile_route_decision"]
                self.assertTrue(route_events)
                self.assertEqual(route_events[-1].payload["source"], "local")
                self.assertTrue(route_events[-1].payload["switched"])
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
                self.assertIn("coding-agent", session.profile_slots)
                self.assertNotIn("general", session.profile_slots)
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["profile"], "coding-agent")

                submitted = conversation.submissions[-1]
                self.assertIn("Turn handling instruction: answer this turn directly", submitted)
                self.assertIn("Do not create or edit files", submitted)
                self.assertIn("User request:\n你是谁", submitted)

                route_events = [event for event in session.event_bus.events if event.type == "profile_route_decision"]
                self.assertTrue(route_events)
                payload = route_events[-1].payload
                self.assertEqual(payload["profile"], "coding-agent")
                self.assertEqual(payload["matched_profile"], "general")
                self.assertEqual(payload["action"], "direct_answer")
                self.assertEqual(payload["turn_mode"], "direct_answer")
                self.assertFalse(payload["switched"])
            finally:
                session.close()

    def test_profile_slash_command_switches_existing_session(self):
        with patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                self.assertTrue(session.is_bound)
                self.assertEqual(session.profile.name(), "general")
                self.assertTrue(session.handle_slash_command("/plan"))

                self.assertEqual(session.profile.name(), "plan")
                self.assertEqual(session.display_profile, "plan")
                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["profile"], "plan")
                self.assertEqual(metadata["initial_profile"], "general")
                self.assertEqual(metadata["profile_source"], "slash command")
            finally:
                session.close()

    def test_resume_slash_command_injects_context_into_existing_session(self):
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
                output = StringIO()
                session.output_sink = lambda text: print(text, file=output)

                self.assertTrue(session.handle_slash_command(f"/resume {previous.id}"))

                self.assertTrue(session.is_bound)
                self.assertEqual(session.resume_session_id, previous.id)
                self.assertIn("injected", output.getvalue())

                metadata = session.session_store.read_metadata(session.session.id)
                self.assertEqual(metadata["resumed_from"], previous.id)
                self.assertIn("Resume context:", conversation.messages[1]["content"])
                self.assertIn(previous.id, conversation.messages[1]["content"])
                self.assertIn("previous task", conversation.messages[1]["content"])
            finally:
                session.close()

    def test_profile_switch_reuses_profile_slots_without_closing_old_context(self):
        coding_conversation = FakeConversation()
        plan_conversation = FakeConversation()
        coding_conversation.response_text = "coding output"
        plan_conversation.response_text = "plan output"

        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            side_effect=[coding_conversation, plan_conversation],
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                session.submit("fix the bug")
                self.assertIs(session.conversation, coding_conversation)

                self.assertTrue(session.handle_slash_command("/plan"))
                self.assertIs(session.conversation, plan_conversation)
                self.assertFalse(coding_conversation.closed)

                self.assertTrue(session.handle_slash_command("/code"))
                self.assertIs(session.conversation, coding_conversation)
                self.assertEqual(coding_conversation.submissions, ["Task:\nfix the bug"])
                self.assertFalse(plan_conversation.closed)
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
                turn_summary = [event for event in session.event_bus.events if event.type == "turn_summary"][0]
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
                    for schema in RecordingInteractiveAgent.init_kwargs["tool_schemas"]
                }

                self.assertIn("read_file", tool_names)
                self.assertIn("mcp__docs__search", tool_names)
                self.assertIsNot(session.tool_registry, tools.BUILTIN_TOOL_REGISTRY)
                self.assertIs(session.tool_context.tool_registry, session.tool_registry)
            finally:
                session.close()

    def test_mcp_slash_commands_show_status_list_and_reload(self):
        class FakeMcpManager:
            instances = []

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

                self.assertTrue(session.handle_slash_command("/mcp status"))
                self.assertTrue(session.handle_slash_command("/mcp list"))
                self.assertTrue(session.handle_slash_command("/mcp reload"))

                text = out.getvalue()
                self.assertIn("server docs: connected", text)
                self.assertIn("mcp__docs__search", text)
                self.assertIn("MCP reloaded", text)
                self.assertTrue(FakeMcpManager.instances[0].closed)
                self.assertGreaterEqual(len(FakeMcpManager.instances), 2)
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
                self.assertIn("## Main-Agent Ownership Rules", prompt)
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
                self.assertEqual(result.checkpoint, "no changes to checkpoint")
                plan_path = Path(self.temp_dir, "global_plan", "current", "plan.md")
                self.assertEqual(plan_path.read_text(encoding="utf-8"), "# Title\n\n## Summary\n\nPlan body\n")
                self.assertFalse(Path(self.temp_dir, ".harness", "sessions", session.session.id, "planning", "state.json").exists())
                self.assertFalse(Path(self.temp_dir, "global_plan", "current", "status.md").exists())
                self.assertFalse(Path(self.temp_dir, "global_plan", "current", "final.md").exists())
            finally:
                session.close()

    def test_plan_continue_switches_to_coding_agent_and_injects_markdown(self):
        plan_conversation = FakeConversation()
        coding_conversation = FakeConversation()
        plan_conversation.response_text = "# Title\n\n## Summary\n\nPlan body"
        coding_conversation.response_text = "implemented"

        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            side_effect=[plan_conversation, coding_conversation],
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="plan")
            try:
                session.submit("plan the parser fix")
                result = session.submit("继续")

                self.assertEqual(result.text, "implemented")
                self.assertEqual(session.profile.name(), "coding-agent")
                self.assertIsNone(session.pending_plan_markdown)
                self.assertEqual(session.pending_plan_revision, 0)
                self.assertEqual(len(coding_conversation.submissions), 1)
                task = coding_conversation.submissions[0]
                self.assertIn("Execute the approved implementation plan", task)
                self.assertNotIn("# Title\n\n## Summary\n\nPlan body", task)
                self.assertEqual(coding_conversation.messages[1]["role"], "user")
                self.assertIn("Profile handoff context:", coding_conversation.messages[1]["content"])
                self.assertIn("Previous profile: plan", coding_conversation.messages[1]["content"])
                self.assertIn("Current profile: coding-agent", coding_conversation.messages[1]["content"])
                self.assertIn("Approved Markdown plan:", coding_conversation.messages[1]["content"])
                self.assertIn("# Title\n\n## Summary\n\nPlan body", coding_conversation.messages[1]["content"])
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

    def test_short_slash_commands_switch_profiles(self):
        conversations = [FakeConversation() for _ in range(7)]
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            side_effect=conversations,
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                cases = [
                    ("/general", "general"),
                    ("/plan", "plan"),
                    ("/code", "coding-agent"),
                    ("/terminal", "terminal"),
                    ("/swe", "swe-bench"),
                    ("/app", "app-builder"),
                    ("/review", "review"),
                ]
                for command, expected in cases:
                    with self.subTest(command=command):
                        self.assertTrue(session.handle_slash_command(command))
                        self.assertEqual(session.profile.name(), expected)
            finally:
                session.close()

    def test_profile_switch_uses_handoff_context_without_copying_old_messages(self):
        coding_conversation = FakeConversation()
        plan_conversation = FakeConversation()
        coding_conversation.response_text = "analysis output"
        with patch(
            "harness_code_agent.agent.conversation.Agent.start_conversation",
            side_effect=[coding_conversation, plan_conversation],
        ):
            session = InteractiveSession(
                cwd=self.temp_dir,
                profile_name="coding-agent",
                profile_explicit=True,
            )
            try:
                session.submit("inspect the auth bug")

                self.assertTrue(session.handle_slash_command("/plan"))

                self.assertEqual(session.profile.name(), "plan")
                self.assertEqual(len(plan_conversation.messages), 2)
                handoff = plan_conversation.messages[1]["content"]
                self.assertIn("Profile handoff context:", handoff)
                self.assertIn("Previous profile: coding-agent", handoff)
                self.assertIn("Current profile: plan", handoff)
                self.assertIn("Most recent user task:", handoff)
                self.assertIn("inspect the auth bug", handoff)
                self.assertIn("Most recent assistant summary:", handoff)
                self.assertIn("analysis output", handoff)
                self.assertNotIn(coding_conversation.submissions[0], handoff)
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

                self.assertTrue(session.handle_slash_command("/code"))

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
        formatted = format_turn_with_mentions("use @file:README.md please", resolved)

        self.assertIn("Mention context:", formatted)
        self.assertIn("resolved as file", formatted)
        self.assertIn("README.md", formatted)
        self.assertIn("Use read_file to inspect this file if needed.", formatted)
        self.assertNotIn("hello docs", formatted)
        self.assertIn("User turn:\nuse @file:README.md please", formatted)

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
        formatted = format_turn_with_mentions("inspect @file:docs", resolved)

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
        from harness_code_agent.core.interactive import print_session

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
        listed = [
            item for item in session.session_store.list_sessions()
            if item.get("id") == session_id
        ][0]
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
                except Exception as exc:
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
        Path(self.temp_dir, "app.py").write_text("print('hi')\n", encoding="utf-8")
        Path(self.temp_dir, ".pytest_cache", "v", "cache").mkdir(parents=True)
        Path(self.temp_dir, ".pytest_cache", "v", "cache", "nodeids").write_text("[]\n", encoding="utf-8")
        Path(self.temp_dir, "__pycache__").mkdir()
        Path(self.temp_dir, "__pycache__", "app.cpython-311.pyc").write_bytes(b"cache")

        dirty = git_dirty_paths(Path(self.temp_dir))

        self.assertIn("app.py", dirty)
        self.assertFalse(any(".pytest_cache" in path for path in dirty))
        self.assertFalse(any("__pycache__" in path for path in dirty))

    def test_hca_first_task_starts_tui_with_first_task(self):
        from harness_code_agent import cli

        class TtyBuffer(StringIO):
            def isatty(self):
                return True

        with (
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch.object(sys, "stdin", TtyBuffer()),
            patch.object(sys, "stdout", TtyBuffer()),
            patch.object(sys, "argv", ["hca", "fix", "tests"]),
        ):
            tui_app.return_value.run.return_value = 0
            result = cli.main()

        self.assertEqual(result, 0)
        tui_app.assert_called_once()
        self.assertEqual(tui_app.call_args.kwargs["first_task"], "fix tests")

    def test_hca_profile_override_starts_tui_with_profile(self):
        from harness_code_agent import cli

        class TtyBuffer(StringIO):
            def isatty(self):
                return True

        with (
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch.object(sys, "stdin", TtyBuffer()),
            patch.object(sys, "stdout", TtyBuffer()),
            patch.object(sys, "argv", ["hca", "--profile", "terminal", "fix", "shell"]),
        ):
            tui_app.return_value.run.return_value = 0
            result = cli.main()

        self.assertEqual(result, 0)
        tui_app.assert_called_once()
        self.assertEqual(tui_app.call_args.kwargs["profile_name"], "terminal")
        self.assertEqual(tui_app.call_args.kwargs["first_task"], "fix shell")

    def test_hca_print_mode_submits_without_tui(self):
        from harness_code_agent import cli

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch.object(sys, "argv", ["hca", "-p", "fix", "tests"]),
        ):
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
        self.assertIn("fix tests", FakeConversation.instances[0].submissions[0])
        tui_app.assert_not_called()

    def test_hca_print_mode_long_flag(self):
        from harness_code_agent import cli

        with (
            patch("harness_code_agent.agent.conversation.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch.object(sys, "argv", ["hca", "--print", "fix", "tests"]),
        ):
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
        tui_app.assert_not_called()

    def test_hca_print_mode_requires_task(self):
        from harness_code_agent import cli

        errors = StringIO()
        with (
            redirect_stderr(errors),
            patch.object(sys, "argv", ["hca", "-p"]),
        ):
            result = cli.main()

        self.assertEqual(result, 2)
        self.assertIn("no task provided", errors.getvalue())

    def test_stream_callback_auto_uses_tty_and_writes_deltas(self):
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

    def test_stream_callback_auto_is_disabled_for_non_tty(self):
        from harness_code_agent import cli

        with patch("harness_code_agent.cli.config.STREAM", "auto"):
            self.assertIsNone(cli._build_stream_callback())

    def test_config_show_includes_sandbox_settings(self):
        from harness_code_agent.core.interactive import format_config_show

        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch.object(config, "DOCKER_IMAGE", "python:3.12"),
            patch.object(config, "DOCKER_NETWORK", "none"),
        ):
            text = format_config_show(Path(self.temp_dir))

        self.assertIn("sandbox_mode: docker", text)
        self.assertIn("docker_image: python:3.12", text)
        self.assertIn("docker_network: none", text)

    def test_config_show_includes_docker_user_and_token_defaults(self):
        from harness_code_agent.core.interactive import format_config_show

        with (
            patch.object(config, "DOCKER_USER", "1000:1000"),
        ):
            text = format_config_show(Path(self.temp_dir))

        self.assertIn("docker_user: 1000:1000", text)
        self.assertIn(f"model_intensity: {config.MODEL_INTENSITY}", text)
        self.assertIn(f"model_profile_fast: {config.MODEL_PROFILES['fast'].model}", text)
        self.assertIn(f"model_profile_hard: {config.MODEL_PROFILES['hard'].model}", text)
        self.assertIn(f"max_agent_total_tokens: {config.MAX_AGENT_TOTAL_TOKENS}", text)

    def test_doctor_docker_mode_with_working_daemon(self):
        from harness_code_agent.core.interactive import format_doctor

        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch("harness_code_agent.core.formatters.docker_cli_path", return_value="/usr/bin/docker"),
            patch("harness_code_agent.core.formatters.docker_info_check", return_value=(True, "Docker 27.0.3")),
        ):
            text, failures = format_doctor(Path(self.temp_dir))

        self.assertIn("Docker CLI", text)
        self.assertIn("Docker daemon", text)
        self.assertIn("Docker 27.0.3", text)
        self.assertEqual(failures, 0)

    def test_doctor_docker_mode_cli_present_daemon_unreachable(self):
        from harness_code_agent.core.interactive import format_doctor

        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch("harness_code_agent.core.formatters.docker_cli_path", return_value="/usr/bin/docker"),
            patch("harness_code_agent.core.formatters.docker_info_check", return_value=(False, "Docker daemon unreachable (exit code 1)")),
        ):
            text, failures = format_doctor(Path(self.temp_dir))

        self.assertIn("FAIL", text)
        self.assertIn("Docker daemon", text)
        self.assertGreater(failures, 0)

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
            patch.object(sys, "argv", ["hca"]),
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
            patch.object(sys, "argv", ["hca"]),
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
