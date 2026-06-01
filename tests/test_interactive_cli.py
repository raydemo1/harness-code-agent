import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


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
        with patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()):
            return InteractiveSession(cwd=self.temp_dir)

    def test_interactive_session_uses_current_directory_workspace(self):
        session = self._session()
        try:
            self.assertEqual(Path(config.WORKSPACE), Path(self.temp_dir))
            self.assertEqual(session.cwd, Path(self.temp_dir).resolve())
            self.assertTrue((Path(self.temp_dir) / ".harness").exists())
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

    def test_cancelled_submit_does_not_emit_assistant_message_event(self):
        from harness_code_agent.agent.cancellation import CancelledError

        class CancellingConversation(FakeConversation):
            def submit(self, task, cancellation_token=None):
                self.submissions.append(task)
                if cancellation_token is not None:
                    cancellation_token.cancel()
                raise CancelledError("Turn cancelled by user")

        with patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=CancellingConversation()):
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
            patch("harness_code_agent.agent.loop.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-permissions")
        try:
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
            patch("harness_code_agent.agent.loop.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
        try:
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
            patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()),
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
            patch("harness_code_agent.agent.loop.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
        try:
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
            patch("harness_code_agent.agent.loop.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
        try:
            prompt = RecordingInteractiveAgent.init_args[1]
            self.assertNotIn("## HARNESS.md", prompt)
        finally:
            session.close()

    def test_plan_profile_captures_markdown_and_offers_handoff_choice(self):
        FakeConversation.response_text = "# Title\n\n## Summary\n\nPlan body"
        with patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()):
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
            "harness_code_agent.agent.loop.Agent.start_conversation",
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

        with patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=plan_conversation):
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
        conversations = [FakeConversation() for _ in range(6)]
        with patch(
            "harness_code_agent.agent.loop.Agent.start_conversation",
            side_effect=conversations,
        ):
            session = InteractiveSession(cwd=self.temp_dir)
            try:
                cases = [
                    ("/plan", "plan"),
                    ("/code", "coding-agent"),
                    ("/terminal", "terminal"),
                    ("/swe", "swe-bench"),
                    ("/app", "app-builder"),
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
            "harness_code_agent.agent.loop.Agent.start_conversation",
            side_effect=[coding_conversation, plan_conversation],
        ):
            session = InteractiveSession(cwd=self.temp_dir)
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

    def test_long_profile_slash_commands_are_not_supported(self):
        session = self._session()
        try:
            self.assertTrue(session.handle_slash_command("/coding-agent"))
            self.assertEqual(session.profile.name(), "coding-agent")
            self.assertTrue(session.handle_slash_command("/swe-bench"))
            self.assertEqual(session.profile.name(), "coding-agent")
        finally:
            session.close()

    def test_switching_profile_clears_pending_plan(self):
        FakeConversation.response_text = "# Title\n\n## Summary\n\nPlan body"
        conversations = [FakeConversation(), FakeConversation()]
        with patch(
            "harness_code_agent.agent.loop.Agent.start_conversation",
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

    def test_file_mention_is_injected(self):
        Path(self.temp_dir, "README.md").write_text("hello docs\n", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        resolved = resolve_mentions(
            "use @README.md please",
            workspace_root=self.temp_dir,
            session_store=store,
        )
        formatted = format_turn_with_mentions("use @README.md please", resolved)

        self.assertIn("Mention context:", formatted)
        self.assertIn("hello docs", formatted)
        self.assertIn("User turn:\nuse @README.md please", formatted)

    def test_missing_file_mention_fails_fast(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")

        with self.assertRaises(MentionResolutionError):
            resolve_mentions(
                "read @missing.md",
                workspace_root=self.temp_dir,
                session_store=store,
            )

    def test_file_mention_rejects_path_escape(self):
        outside = Path(self.temp_dir).parent / "outside-mention.txt"
        outside.write_text("nope", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        try:
            with self.assertRaises(MentionResolutionError):
                resolve_mentions(
                    "read @../outside-mention.txt",
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
        session_id = session.session.id
        try:
            session.submit("summarize this session")
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

    def test_clean_checkpoint_is_skipped(self):
        session = self._session()
        try:
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
            Path(self.temp_dir, "new.txt").write_text("new\n", encoding="utf-8")

            result = session.create_checkpoint(manual=False, baseline_dirty=baseline)

            self.assertIn("checkpoint created:", result)
            self.assertIn("?? preexisting.txt", self._git("status", "--porcelain", "--", "preexisting.txt"))
            self.assertEqual(self._git("status", "--porcelain", "--", "new.txt"), "")
        finally:
            session.close()

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
            patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()),
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
            patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.cli.TuiApp") as tui_app,
            patch.object(sys, "argv", ["hca", "--print", "fix", "tests"]),
        ):
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
        tui_app.assert_not_called()

    def test_hca_print_mode_requires_task(self):
        from harness_code_agent import cli

        output = StringIO()
        with (
            redirect_stdout(output),
            patch.object(sys, "argv", ["hca", "-p"]),
        ):
            result = cli.main()

        self.assertEqual(result, 2)
        self.assertIn("no task provided", output.getvalue())

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

    def test_print_turn_result_does_not_duplicate_streamed_text(self):
        from harness_code_agent.core.interactive import TurnResult, print_turn_result

        output = StringIO()
        with redirect_stdout(output):
            print_turn_result(TurnResult(text="already streamed", checkpoint="checkpoint", streamed=True))

        self.assertNotIn("already streamed", output.getvalue())
        self.assertIn("checkpoint", output.getvalue())

    def test_hca_interactive_requires_tty(self):
        from harness_code_agent import cli

        output = StringIO()
        with (
            redirect_stdout(output),
            patch.object(sys, "argv", ["hca"]),
            patch.object(sys.stdin, "read", return_value=""),
        ):
            result = cli.main()

        self.assertEqual(result, 2)
        self.assertIn("no task provided", output.getvalue())

    def test_hca_no_tty_auto_degrades_to_batch(self):
        from harness_code_agent import cli

        with (
            patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()),
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
