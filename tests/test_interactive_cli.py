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

    def submit(self, task):
        self.submissions.append(task)
        return self.response_text

    def close(self):
        self.closed = True


class RecordingInteractiveAgent:
    init_kwargs = None

    def __init__(self, *args, **kwargs):
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

    def test_legacy_core_harness_controller_is_removed(self):
        project_root = Path(__file__).resolve().parents[1]

        self.assertFalse((project_root / "harness_code_agent" / "core" / "harness.py").exists())

    def test_initial_task_path_submits_to_live_conversation(self):
        session = self._session()
        try:
            result = session.submit("fix the tests")
            self.assertEqual(result.text, "assistant done")
            self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
            self.assertIn("fix the tests", FakeConversation.instances[0].submissions[0])
        finally:
            session.close()

    def test_interactive_session_passes_profile_tool_schemas_to_agent(self):
        class FakeToolProfile:
            def name(self):
                return "fake-tools"

            def main_agent(self):
                return AgentConfig(
                    system_prompt="fake",
                    tool_schemas=[{"type": "function", "function": {"name": "read_file"}}],
                )

        with (
            patch("harness_code_agent.core.interactive.get_profile", return_value=FakeToolProfile()),
            patch("harness_code_agent.agent.loop.Agent", RecordingInteractiveAgent),
        ):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="fake-tools")
        try:
            self.assertEqual(
                RecordingInteractiveAgent.init_kwargs["tool_schemas"],
                [{"type": "function", "function": {"name": "read_file"}}],
            )
        finally:
            session.close()

    def test_plan_profile_captures_markdown_and_offers_handoff_choice(self):
        FakeConversation.response_text = "# Title\n\n## Summary\n\nPlan body"
        with patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()):
            session = InteractiveSession(cwd=self.temp_dir, profile_name="plan")
        try:
            result = session.submit("plan the parser fix")

            self.assertEqual(session.pending_plan_markdown, "# Title\n\n## Summary\n\nPlan body")
            self.assertIn("Say 'continue'", result.notice)
            self.assertIn("reply with feedback", result.notice)
            self.assertEqual(result.checkpoint, "no changes to checkpoint")
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
        bus.emit("after_tool", agent="main_agent", payload={"tool": "read_file", "ok": True})
        bus.emit("file_changed", agent="main_agent", payload={"path": "app.py"})

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
        self.assertIn("Session summary", text)
        self.assertIn(f"id: {session_id}", text)
        self.assertIn("status: closed", text)
        self.assertIn("task_outcome: unknown", text)

        events = session.session_store.read_events(session_id)
        self.assertEqual(events[-1]["type"], "session_finished")
        self.assertEqual(events[-1]["payload"]["reason"], "user_exit")
        self.assertEqual(events[-1]["payload"]["status"], "closed")
        self.assertNotIn("task_outcome", [event["type"] for event in events])

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

    def test_old_run_command_is_rejected(self):
        from harness_code_agent import cli

        self.assertEqual(cli.main(["run", "fix tests"]), 1)

    def test_top_level_harness_py_wrapper_is_removed(self):
        project_root = Path(__file__).resolve().parents[1]

        self.assertFalse((project_root / "harness.py").exists())

    def test_hca_first_task_submits_then_repl_can_exit(self):
        from harness_code_agent import cli

        with (
            patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.cli._build_prompt", return_value=lambda: "/exit"),
            patch.object(sys, "argv", ["hca", "fix", "tests"]),
        ):
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
        self.assertIn("fix tests", FakeConversation.instances[0].submissions[0])

    def test_hca_profile_override_submits_first_task_through_interactive_session(self):
        from harness_code_agent import cli

        with (
            patch("harness_code_agent.agent.loop.Agent.start_conversation", return_value=FakeConversation()),
            patch("harness_code_agent.cli._build_prompt", return_value=lambda: "/exit"),
            patch.object(sys, "argv", ["hca", "--profile", "terminal", "fix", "shell"]),
        ):
            result = cli.main()

        self.assertEqual(result, 0)
        self.assertEqual(len(FakeConversation.instances[0].submissions), 1)
        self.assertIn("fix shell", FakeConversation.instances[0].submissions[0])


if __name__ == "__main__":
    unittest.main()
