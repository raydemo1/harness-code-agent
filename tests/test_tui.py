import json
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
from harness_code_agent.tui.approval import (
    ApprovalAllowlist,
    ApprovalChoiceBar,
    TuiApprovalProvider,
    _derive_persistent_prefix,
    _format_approval_body,
)
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

    def test_approval_requested_summary_omits_repeated_args_blob(self):
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

        block = state.apply_event(
            {
                "type": "approval_requested",
                "payload": {
                    "tool": "run_bash",
                    "risk": "shell_dangerous",
                    "reason": "workspace-write mode requires user approval for high-risk shell commands",
                    "args": {"command": "rm -rf build", "content": "x" * 500},
                },
            }
        )

        self.assertIsNotNone(block)
        self.assertEqual(block.status, "pending")
        self.assertIn("tool=run_bash", block.body)
        self.assertIn("risk=shell_dangerous", block.body)
        self.assertIn("reason=workspace-write mode requires user approval", block.body)
        self.assertNotIn("args=", block.body)
        self.assertNotIn("content=", block.body)

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
        class FakeChoiceBar:
            answers = ["approve", "deny"]

            def __init__(self, request, *, show_details=False, **_kwargs):
                self.request = request
                self.show_details = show_details

            def run(self):
                return self.answers.pop(0)

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        provider = TuiApprovalProvider(choice_bar_factory=FakeChoiceBar)
        approved = provider.request(request)
        denied = provider.request(request)

        self.assertTrue(approved.approved)
        self.assertFalse(denied.approved)

    def test_tui_approval_provider_persists_project_prefix_rule_and_reuses_it(self):
        class FakeChoiceBar:
            calls = 0

            def __init__(self, request, *, show_details=False, persistent_prefix=None, **_kwargs):
                self.request = request
                self.show_details = show_details
                self.persistent_prefix = persistent_prefix

            def run(self):
                self.__class__.calls += 1
                return "persist"

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "npm run test -- --watch"},
            risk="shell_risky",
            reason="read-only mode requires user approval",
        )
        provider = TuiApprovalProvider(project_root=self.root, choice_bar_factory=FakeChoiceBar)

        first = provider.request(request)
        second = provider.request(
            ApprovalRequest(
                tool_name="run_bash",
                args={"command": "npm run test -- tests/test_tui.py"},
                risk="shell_risky",
                reason="read-only mode requires user approval",
            )
        )
        data = json.loads((self.root / ".harness" / "approval_allowlist.json").read_text(encoding="utf-8"))

        self.assertTrue(first.approved)
        self.assertTrue(second.approved)
        self.assertEqual(FakeChoiceBar.calls, 1)
        self.assertEqual(data["rules"][0]["prefix"], ["npm", "run", "test"])
        self.assertEqual(second.metadata["approval_source"], "project_allowlist")

    def test_python_command_does_not_persist_bare_prefix(self):
        self.assertIsNone(_derive_persistent_prefix("python"))
        self.assertIsNone(_derive_persistent_prefix("python script.py"))
        self.assertEqual(_derive_persistent_prefix("python -m unittest tests.test_tui"), ["python", "-m", "unittest"])

    def test_project_allowlist_does_not_match_other_project(self):
        allowlist = ApprovalAllowlist(self.root)
        allowlist.add_prefix_rule(["npm", "run", "test"], command="npm run test -- --watch")
        other_project = Path(tempfile.mkdtemp())
        try:
            other_allowlist = ApprovalAllowlist(other_project)

            self.assertFalse(other_allowlist.matches("npm run test -- tests/test_tui.py"))
        finally:
            shutil.rmtree(other_project, ignore_errors=True)

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
        class FakeChoiceBarEOF:
            def __init__(self, request, *, show_details=False, **_kwargs):
                self.request = request
                self.show_details = show_details

            def run(self):
                raise EOFError()

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        with patch("harness_code_agent.tui.approval.print_formatted_text"):
            provider = TuiApprovalProvider(choice_bar_factory=FakeChoiceBarEOF)
            result = provider.request(request)

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "interrupted in TUI")

    def test_tui_approval_provider_keyboard_interrupt(self):
        class FakeChoiceBarKI:
            def __init__(self, request, *, show_details=False, **_kwargs):
                self.request = request
                self.show_details = show_details

            def run(self):
                raise KeyboardInterrupt()

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            risk="high",
            reason="test",
        )
        with patch("harness_code_agent.tui.approval.print_formatted_text"):
            provider = TuiApprovalProvider(choice_bar_factory=FakeChoiceBarKI)
            result = provider.request(request)

        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "interrupted in TUI")

    def test_tui_approval_provider_details_then_approve(self):
        class FakeChoiceBarDetails:
            answers = ["details", "approve"]
            detail_states = []

            def __init__(self, request, *, show_details=False, **_kwargs):
                self.request = request
                self.show_details = show_details
                self.detail_states.append(show_details)

            def run(self):
                return self.answers.pop(0)

        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "rm -rf build", "content": "secret text"},
            risk="high",
            reason="test",
        )
        provider = TuiApprovalProvider(choice_bar_factory=FakeChoiceBarDetails)
        result = provider.request(request)

        self.assertTrue(result.approved)
        self.assertEqual(FakeChoiceBarDetails.detail_states, [False, True])

    def test_tui_approval_panel_includes_risk_reason_and_command(self):
        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "rm -rf build"},
            risk="shell_dangerous",
            reason="workspace-write mode requires user approval",
        )

        body = _format_approval_body(request, show_details=False)

        self.assertIn("Approval required", body)
        self.assertIn("tool: run_bash", body)
        self.assertIn("risk: shell_dangerous", body)
        self.assertIn("reason: workspace-write mode requires user approval", body)
        self.assertIn("command: rm -rf build", body)

    def test_tui_approval_choice_bar_defaults_to_approve(self):
        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "rm -rf build"},
            risk="shell_dangerous",
            reason="workspace-write mode requires user approval",
        )

        choice_bar = ApprovalChoiceBar(request)

        self.assertEqual(choice_bar.selected_index, 0)

    def test_tui_approval_panel_details_expands_args(self):
        request = ApprovalRequest(
            tool_name="write_file",
            args={"path": "note.txt", "content": "hello"},
            risk="edit",
            reason="read-only mode requires user approval",
        )

        collapsed = _format_approval_body(request, show_details=False)
        expanded = _format_approval_body(request, show_details=True)

        self.assertIn("content: [5 chars]", collapsed)
        self.assertNotIn("'content': 'hello'", collapsed)
        self.assertIn("'content': 'hello'", expanded)
        self.assertIn("'path': 'note.txt'", expanded)

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
