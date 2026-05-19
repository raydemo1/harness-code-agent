import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
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


class FakeConversation:
    instances = []

    def __init__(self):
        self.messages = [{"role": "system", "content": "fake"}]
        self.submissions = []
        self.closed = False
        self.__class__.instances.append(self)

    def submit(self, task):
        self.submissions.append(task)
        return "assistant done"

    def close(self):
        self.closed = True


class InteractiveCliTests(unittest.TestCase):
    def setUp(self):
        self.old_workspace = config.WORKSPACE
        self.old_api_key = config.API_KEY
        self.old_cwd = os.getcwd()
        self.temp_dir = tempfile.mkdtemp()
        config.API_KEY = "test-key"
        FakeConversation.instances = []
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
        self.assertIn("session_started", resolved[0].content)

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

    def test_harness_core_main_rejects_old_run_command(self):
        from harness_code_agent.core import harness

        with patch.object(sys, "argv", ["harness.py", "run", "fix tests"]):
            with self.assertRaises(SystemExit) as raised:
                harness.main()

        self.assertEqual(raised.exception.code, 1)

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


if __name__ == "__main__":
    unittest.main()
