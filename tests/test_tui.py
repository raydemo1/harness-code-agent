import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness_code_agent.core.mentions import parse_mentions, resolve_mentions
from harness_code_agent.runtime.approvals import ApprovalRequest
from harness_code_agent.sessions.events import EventBus
from harness_code_agent.sessions.store import SessionStore
from harness_code_agent.tui.approval import TuiApprovalProvider
from harness_code_agent.tui.commands import default_command_registry
from harness_code_agent.tui.completion import current_mention_query, mention_candidates
from harness_code_agent.tui.render import prompt_message
from harness_code_agent.tui.state import SessionStatusSnapshot, TuiState


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_command_registry_groups_commands_and_switches_profile(self):
        registry = default_command_registry()
        calls = []
        session = SimpleNamespace(switch_profile=lambda name: calls.append(name) or f"switched {name}")

        help_text = registry.format_help()
        result = registry.execute("/code", session)

        self.assertIn("Profiles:", help_text)
        self.assertIn("/checkpoint", help_text)
        self.assertEqual(result.text, "switched coding-agent")
        self.assertEqual(calls, ["coding-agent"])

    def test_command_registry_reports_validation_errors(self):
        registry = default_command_registry()
        result = registry.execute("/config nope", SimpleNamespace())

        self.assertIn("Usage: /config show", result.text)
        self.assertTrue(result.should_continue)

    def test_quoted_file_mention_resolves_paths_with_spaces(self):
        Path(self.temp_dir, "space name.md").write_text("hello space\n", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        mentions = parse_mentions('read @"space name.md" now')
        resolved = resolve_mentions(
            'read @"space name.md" now',
            workspace_root=self.temp_dir,
            session_store=store,
        )

        self.assertEqual(mentions[0].target, "space name.md")
        self.assertEqual(resolved[0].content, "hello space\n")

    def test_mention_candidates_fuzzy_files_sessions_and_exclusions(self):
        Path(self.temp_dir, "space name.md").write_text("hello\n", encoding="utf-8")
        Path(self.temp_dir, "node_modules").mkdir()
        Path(self.temp_dir, "node_modules", "ignored.js").write_text("x\n", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="plan",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )

        file_candidates = mention_candidates(self.root, "space", store)
        session_candidates = mention_candidates(self.root, "session:" + session.id[:8], store)

        self.assertIn('"space name.md"', [item.insert_text for item in file_candidates])
        self.assertNotIn("node_modules/ignored.js", [item.insert_text for item in file_candidates])
        self.assertIn("session:" + session.id, [item.insert_text for item in session_candidates])

    def test_current_mention_query_handles_plain_and_quoted_mentions(self):
        self.assertEqual(current_mention_query("fix @READ"), ("READ", -5))
        self.assertEqual(current_mention_query('fix @"space'), ("space", -7))
        self.assertIsNone(current_mention_query("email@domain"))

    def test_event_bus_listener_updates_tui_state(self):
        events = []
        bus = EventBus(listener=events.append)
        state = TuiState(
            SessionStatusSnapshot(
                profile="coding-agent",
                model="model-a",
                provider="auto",
                permission_mode="workspace-write",
                session_id="s1",
                cwd=self.root,
            )
        )

        bus.emit("tool_call", agent="main_agent", payload={"tool": "read_file", "args": {"path": "README.md"}})
        bus.emit("tool_result", agent="main_agent", payload={"tool": "read_file", "status": "success", "output": "ok"})
        bus.emit(
            "plan_ready",
            agent="main_agent",
            payload={
                "profile": "plan",
                "plan_path": "global_plan/current/plan.md",
                "plan_revision": 1,
                "approval_source": "/plan",
            },
        )
        bus.emit("profile_switched", agent="main_agent", payload={"previous_profile": "plan", "profile": "coding-agent", "reason": "execute"})
        bus.emit("turn_finished", agent="main_agent", payload={"turn": 1, "checkpoint": "checkpoint created: abc"})

        for event in events:
            state.add_block(state.apply_event(event))

        self.assertEqual(state.snapshot.profile, "coding-agent")
        self.assertFalse(state.snapshot.pending_plan)
        self.assertEqual(state.snapshot.checkpoint, "checkpoint created: abc")
        self.assertEqual(state.snapshot.running_tool, "")
        self.assertGreaterEqual(len(state.blocks), 5)

    def test_pending_plan_prompt_renders_action_bar_labels(self):
        snapshot = SessionStatusSnapshot(
            profile="plan",
            model="model-a",
            provider="auto",
            permission_mode="workspace-write",
            session_id="s1",
            cwd=self.root,
            pending_plan=True,
        )

        prompt = str(prompt_message(snapshot))

        self.assertIn("执行计划", prompt)
        self.assertIn("修改计划", prompt)
        self.assertIn("输入修改理由", prompt)

    def test_tui_approval_provider_approve_and_deny(self):
        class FakePromptSession:
            answers = ["y", "n"]

            def prompt(self, *_args, **_kwargs):
                return self.answers.pop(0)

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        with (
            patch("harness_code_agent.tui.approval.PromptSession", FakePromptSession),
            patch("harness_code_agent.tui.approval.print_formatted_text"),
        ):
            provider = TuiApprovalProvider()
            approved = provider.request(request)
            denied = provider.request(request)

        self.assertTrue(approved.approved)
        self.assertFalse(denied.approved)

    def test_double_at_mention_is_ignored(self):
        mentions = parse_mentions("use @@file please")
        self.assertEqual(len(mentions), 0)

    def test_event_bus_listener_exception_logged(self):
        def bad_listener(event):
            raise ValueError("rendering error")
        
        bus = EventBus(listener=bad_listener)
        with patch("harness_code_agent.sessions.events.log") as mock_log:
            bus.emit("user_input", payload={"text": "hello"})
            mock_log.debug.assert_called_once()
            args, _ = mock_log.debug.call_args
            self.assertIn("EventBus listener error", args[0])

    def test_tui_approval_provider_eof_error(self):
        class FakePromptSessionEOF:
            def prompt(self, *_args, **_kwargs):
                raise EOFError()

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        with (
            patch("harness_code_agent.tui.approval.PromptSession", FakePromptSessionEOF),
            patch("harness_code_agent.tui.approval.print_formatted_text"),
        ):
            provider = TuiApprovalProvider()
            result = provider.request(request)

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "interrupted in TUI")

    def test_tui_approval_provider_keyboard_interrupt(self):
        class FakePromptSessionKI:
            def prompt(self, *_args, **_kwargs):
                raise KeyboardInterrupt()

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        with (
            patch("harness_code_agent.tui.approval.PromptSession", FakePromptSessionKI),
            patch("harness_code_agent.tui.approval.print_formatted_text"),
        ):
            provider = TuiApprovalProvider()
            result = provider.request(request)

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "interrupted in TUI")

    def test_tui_approval_provider_invalid_input_hint(self):
        class FakePromptSessionInvalid:
            answers = ["invalid_arg", "y"]
            def prompt(self, *_args, **_kwargs):
                return self.answers.pop(0)

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        with (
            patch("harness_code_agent.tui.approval.PromptSession", FakePromptSessionInvalid),
            patch("harness_code_agent.tui.approval.print_formatted_text") as mock_print,
        ):
            provider = TuiApprovalProvider()
            result = provider.request(request)

        self.assertTrue(result.approved)
        printed = [arg[0] for arg, _ in mock_print.call_args_list]
        self.assertTrue(any("Invalid input" in str(p) for p in printed))

    def test_iter_workspace_paths_skips_excluded_dirs_traversal(self):
        import os
        from harness_code_agent.tui.completion import iter_workspace_paths
        
        node_modules_dir = Path(self.temp_dir) / "node_modules"
        node_modules_dir.mkdir()
        (node_modules_dir / "hidden.js").write_text("console.log('hi')", encoding="utf-8")
        
        src_dir = Path(self.temp_dir) / "src"
        src_dir.mkdir()
        (src_dir / "app.js").write_text("console.log('app')", encoding="utf-8")
        
        scanned_dirs = []
        original_scandir = os.scandir
        
        def spy_scandir(path):
            scanned_dirs.append(Path(path).resolve())
            return original_scandir(path)
            
        with patch("os.scandir", side_effect=spy_scandir):
            paths = list(iter_workspace_paths(Path(self.temp_dir)))
            
        # Verify src is in the returned list
        rel_paths = [p[0] for p in paths]
        self.assertIn("src", rel_paths)
        self.assertIn("src/app.js", rel_paths)
        
        # Verify node_modules content is NOT in the returned list
        self.assertNotIn("node_modules/hidden.js", rel_paths)
        
        # Verify os.scandir was never called on the node_modules directory!
        node_modules_resolved = node_modules_dir.resolve()
        self.assertNotIn(node_modules_resolved, scanned_dirs)






if __name__ == "__main__":
    unittest.main()
