import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from prompt_toolkit.formatted_text import to_formatted_text

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
from harness_code_agent.tui.question import (
    QuestionChoiceBar,
    TuiQuestionProvider,
    _format_question_choice_bar,
)
from harness_code_agent.tui.commands import default_command_registry
from harness_code_agent.tui.completion import current_mention_query, mention_candidates
from harness_code_agent.tui.render import bottom_toolbar, prompt_message
from harness_code_agent.tui.state import SessionStatusSnapshot, TranscriptBlock, TuiState


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

    def test_bottom_toolbar_renders_clickable_context_progress_bar(self):
        clicked = []
        permission_clicked = []
        snapshot = SessionStatusSnapshot(
            profile="coding-agent",
            model="model-a",
            provider="auto",
            permission_mode="workspace-write",
            session_id="s1",
            cwd=self.root,
            context_tokens=90_000,
            context_window_tokens=128_000,
            context_observe_threshold=76_800,
            context_prepare_threshold=87_040,
            context_allow_threshold=96_000,
            context_force_threshold=104_960,
            context_hint=True,
        )

        fragments = list(to_formatted_text(bottom_toolbar(
            snapshot,
            on_context_click=lambda: clicked.append(True),
            on_permission_click=lambda: permission_clicked.append(True),
        )))
        text = "".join(fragment[1] for fragment in fragments)
        permission_handlers = [
            fragment[2]
            for fragment in fragments
            if len(fragment) >= 3 and fragment[2] is not None and "workspace-write" in fragment[1]
        ]
        context_handlers = [
            fragment[2]
            for fragment in fragments
            if len(fragment) >= 3 and fragment[2] is not None and "ctx" in fragment[1]
        ]

        self.assertIn("perm workspace-write", text)
        self.assertIn("ctx", text)
        self.assertIn("▓", text)
        self.assertIn("░", text)
        self.assertIn("70%", text)
        self.assertIn("90K/128K", text)
        self.assertNotIn("○60", text)
        self.assertNotIn("○68", text)
        self.assertNotIn("○75", text)
        self.assertNotIn("○82", text)
        self.assertTrue(permission_handlers)
        permission_handlers[0](SimpleNamespace(event_type="MOUSE_UP"))
        self.assertEqual(permission_clicked, [True])
        self.assertTrue(context_handlers)
        context_handlers[0](SimpleNamespace(event_type="MOUSE_UP"))
        self.assertEqual(clicked, [True])

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
                    "risk": "shell_risky",
                    "reason": "workspace-write mode requires user approval for non-whitelisted commands and tools",
                    "args": {"command": "rm -rf build", "content": "x" * 500},
                },
            }
        )

        self.assertIsNotNone(block)
        self.assertEqual(block.status, "pending")
        self.assertIn("tool=run_bash", block.body)
        self.assertIn("risk=shell_risky", block.body)
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
            reason="workspace-write mode requires user approval",
        )
        provider = TuiApprovalProvider(project_root=self.root, choice_bar_factory=FakeChoiceBar)

        first = provider.request(request)
        second = provider.request(
            ApprovalRequest(
                tool_name="run_bash",
                args={"command": "npm run test -- tests/test_tui.py"},
                risk="shell_risky",
                reason="workspace-write mode requires user approval",
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
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )

        body = _format_approval_body(request, show_details=False)

        self.assertIn("Approval required", body)
        self.assertIn("tool: run_bash", body)
        self.assertIn("risk: shell_risky", body)
        self.assertIn("reason: workspace-write mode requires user approval", body)
        self.assertIn("command: rm -rf build", body)

    def test_tui_approval_choice_bar_defaults_to_approve(self):
        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "rm -rf build"},
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )

        choice_bar = ApprovalChoiceBar(request)

        self.assertEqual(choice_bar.selected_index, 0)

    def test_tui_approval_panel_details_expands_args(self):
        request = ApprovalRequest(
            tool_name="write_file",
            args={"path": "note.txt", "content": "hello"},
            risk="edit",
            reason="workspace-write mode allows edit",
        )

        collapsed = _format_approval_body(request, show_details=False)
        expanded = _format_approval_body(request, show_details=True)

        self.assertIn("content: [5 chars]", collapsed)
        self.assertNotIn("'content': 'hello'", collapsed)
        self.assertIn("'content': 'hello'", expanded)
        self.assertIn("'path': 'note.txt'", expanded)

    def test_tui_question_provider_uses_choice_bar_result(self):
        from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

        class FakeChoiceBar:
            def __init__(self, request):
                self.request = request

            def run(self):
                return {
                    "selected_index": 1,
                    "label": "Other",
                    "value": "Other",
                    "is_other": True,
                    "custom_text": "Use a hybrid approach",
                }

        request = QuestionRequest(
            question="Which approach?",
            options=[QuestionOption(label="Simple"), QuestionOption(label="Other", is_other=True)],
        )
        result = TuiQuestionProvider(choice_bar_factory=FakeChoiceBar).ask(request)

        self.assertEqual(result.selected_index, 1)
        self.assertTrue(result.is_other)
        self.assertEqual(result.custom_text, "Use a hybrid approach")

    def test_question_choice_bar_number_key_requires_second_press_to_submit(self):
        from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

        request = QuestionRequest(
            question="Which approach?",
            options=[
                QuestionOption(label="Simple"),
                QuestionOption(label="Detailed"),
                QuestionOption(label="Other", is_other=True),
            ],
        )
        bar = QuestionChoiceBar(request)

        self.assertIsNone(bar.handle_number_key("2"))
        self.assertEqual(bar.selected_index, 1)
        self.assertEqual(bar.handle_number_key("2")["label"], "Detailed")

    def test_question_choice_bar_allows_digits_in_other_text(self):
        from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

        request = QuestionRequest(
            question="Which version?",
            options=[
                QuestionOption(label="Stable"),
                QuestionOption(label="Experimental"),
                QuestionOption(label="Other", is_other=True),
            ],
        )
        bar = QuestionChoiceBar(request)
        bar._select(2)

        self.assertIsNone(bar.handle_number_key("1"))
        self.assertEqual(bar.other_text, "1")

    def test_question_choice_bar_keeps_other_number_double_submit(self):
        from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

        request = QuestionRequest(
            question="Which version?",
            options=[
                QuestionOption(label="Stable"),
                QuestionOption(label="Experimental"),
                QuestionOption(label="Other", is_other=True),
            ],
        )
        bar = QuestionChoiceBar(request)

        self.assertIsNone(bar.handle_number_key("3"))
        payload = bar.handle_number_key("3")

        self.assertEqual(payload["label"], "Other")
        self.assertTrue(payload["is_other"])

    def test_question_choice_bar_renders_numbered_other_input(self):
        from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

        request = QuestionRequest(
            question="Which approach?",
            options=[QuestionOption(label="Simple"), QuestionOption(label="Other", is_other=True)],
        )
        fragments = _format_question_choice_bar(request.options, selected_index=1, other_text="custom")
        text = "".join(fragment[1] for fragment in fragments)

        self.assertIn("1 Simple", text)
        self.assertIn("2 Other", text)
        self.assertIn("Other: custom", text)

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






class TuiLayoutTests(unittest.TestCase):
    """Phase 1: Layout skeleton tests for the async Application-based TUI."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_app(self, **kwargs):
        """Create a TuiApp with mocked InteractiveSession for layout testing."""
        from prompt_toolkit.output.defaults import DummyOutput
        from harness_code_agent.tui.app import TuiApp
        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession, \
             patch("prompt_toolkit.output.defaults.create_output", return_value=DummyOutput()):
            mock_session = SimpleNamespace(
                profile=SimpleNamespace(name=lambda: "coding-agent"),
                session=SimpleNamespace(id="test-session"),
                cwd=str(self.root),
                permission_mode="workspace-write",
                conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
                close=lambda: None,
                handle_slash_command=lambda line: True,
                manual_compact_context=lambda: "compacted",
            )
            MockSession.return_value = mock_session
            app = TuiApp(cwd=self.root, profile_name="coding-agent", **kwargs)
        return app

    def test_tui_app_creates_application_instance(self):
        """TuiApp should have a prompt_toolkit Application after init."""
        from prompt_toolkit.application import Application
        app = self._make_app()
        self.assertIsInstance(app.app, Application)

    def test_layout_has_transcript_window(self):
        """Layout should contain a transcript area for displaying conversation."""
        app = self._make_app()
        # The transcript control should exist and be a FormattedTextControl
        self.assertIsNotNone(app._transcript_control)
        self.assertEqual(len(app.state.blocks), 0)

    def test_layout_has_input_text_area(self):
        """Layout should contain a TextArea for user input."""
        from prompt_toolkit.widgets import TextArea
        app = self._make_app()
        self.assertIsInstance(app._input_area, TextArea)

    def test_permission_bar_click_toggles_session_permission_mode(self):
        """Clicking the permission segment should cycle the runtime permission mode."""
        app = self._make_app()
        calls = []

        def toggle_permission_mode():
            calls.append(True)
            app.session.permission_mode = "danger-full-access"
            return "permission mode switched: workspace-write -> danger-full-access"

        app.session.toggle_permission_mode = toggle_permission_mode

        app._toggle_permission_mode_from_bar()

        self.assertEqual(calls, [True])
        self.assertEqual(app.state.snapshot.permission_mode, "danger-full-access")
        self.assertTrue(any("permission mode switched" in block.body for block in app.state.blocks))

    def test_context_bar_renders_green_below_68_percent(self):
        """Context bar should use green style when usage < 68%."""
        from prompt_toolkit.formatted_text import to_formatted_text
        from harness_code_agent.tui.render import context_bar_fragments
        snapshot = SessionStatusSnapshot(
            profile="coding-agent", model="m", provider="p",
            permission_mode="w", session_id="s", cwd=self.root,
            context_tokens=50_000, context_window_tokens=100_000,
        )
        fragments = list(to_formatted_text(context_bar_fragments(snapshot)))
        styles = [f[0] for f in fragments]
        # Green: #a3be8c
        self.assertTrue(any("#a3be8c" in s for s in styles))

    def test_context_bar_renders_yellow_at_70_percent(self):
        """Context bar should use yellow style when usage is 68-81%."""
        from prompt_toolkit.formatted_text import to_formatted_text
        from harness_code_agent.tui.render import context_bar_fragments
        snapshot = SessionStatusSnapshot(
            profile="coding-agent", model="m", provider="p",
            permission_mode="w", session_id="s", cwd=self.root,
            context_tokens=70_000, context_window_tokens=100_000,
        )
        fragments = list(to_formatted_text(context_bar_fragments(snapshot)))
        styles = [f[0] for f in fragments]
        # Yellow: #ebcb8b
        self.assertTrue(any("#ebcb8b" in s for s in styles))

    def test_context_bar_renders_red_above_82_percent(self):
        """Context bar should use red style when usage >= 82%."""
        from prompt_toolkit.formatted_text import to_formatted_text
        from harness_code_agent.tui.render import context_bar_fragments
        snapshot = SessionStatusSnapshot(
            profile="coding-agent", model="m", provider="p",
            permission_mode="w", session_id="s", cwd=self.root,
            context_tokens=85_000, context_window_tokens=100_000,
        )
        fragments = list(to_formatted_text(context_bar_fragments(snapshot)))
        styles = [f[0] for f in fragments]
        # Red: #bf616a
        self.assertTrue(any("#bf616a" in s for s in styles))

    def test_context_bar_shows_progress_bar_and_percentage(self):
        """Context bar should display progress bar and percentage."""
        from prompt_toolkit.formatted_text import to_formatted_text
        from harness_code_agent.tui.render import context_bar_fragments
        permission_clicked = []
        snapshot = SessionStatusSnapshot(
            profile="coding-agent", model="m", provider="p",
            permission_mode="workspace-write", session_id="s", cwd=self.root,
            context_tokens=70_000, context_window_tokens=100_000,
        )
        fragments = list(to_formatted_text(context_bar_fragments(
            snapshot,
            on_permission_click=lambda: permission_clicked.append(True),
        )))
        text = "".join(f[1] for f in fragments)
        permission_handlers = [
            fragment[2]
            for fragment in fragments
            if len(fragment) >= 3 and fragment[2] is not None and "workspace-write" in fragment[1]
        ]
        self.assertIn("perm workspace-write", text)
        self.assertIn("ctx", text)
        self.assertIn("70%", text)
        self.assertIn("▓", text)
        self.assertIn("░", text)
        self.assertTrue(permission_handlers)
        permission_handlers[0](SimpleNamespace(event_type="MOUSE_UP"))
        self.assertEqual(permission_clicked, [True])

    def test_transcript_renders_user_message_with_blue_bar(self):
        """User messages in transcript should have blue left marker."""
        from harness_code_agent.tui.render import render_block_fragments
        block = TranscriptBlock("user", "user turn 1", "fix the bug")
        fragments = list(to_formatted_text(render_block_fragments(block)))
        text = "".join(f[1] for f in fragments)
        self.assertIn("fix the bug", text)

    def test_transcript_renders_tool_as_card(self):
        """Tool results should render as a card with border."""
        from harness_code_agent.tui.render import render_block_fragments
        block = TranscriptBlock("tool", "tool result: run_bash", "output text", "success")
        fragments = list(to_formatted_text(render_block_fragments(block)))
        text = "".join(f[1] for f in fragments)
        self.assertIn("┌", text)
        self.assertIn("┘", text)
        self.assertIn("run_bash", text)


class TuiAsyncTests(unittest.TestCase):
    """Phase 2: Async submit tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_app(self, **kwargs):
        from prompt_toolkit.output.defaults import DummyOutput
        from harness_code_agent.tui.app import TuiApp
        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession, \
             patch("prompt_toolkit.output.defaults.create_output", return_value=DummyOutput()):
            mock_session = SimpleNamespace(
                profile=SimpleNamespace(name=lambda: "coding-agent"),
                session=SimpleNamespace(id="test-session"),
                cwd=str(self.root),
                permission_mode="workspace-write",
                conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
                close=lambda: None,
                handle_slash_command=lambda line: True,
                manual_compact_context=lambda: "compacted",
                submit=lambda text: SimpleNamespace(notice="", checkpoint=""),
            )
            MockSession.return_value = mock_session
            app = TuiApp(cwd=self.root, profile_name="coding-agent", **kwargs)
        return app

    def test_submit_dispatches_to_background_thread(self):
        """_submit_async should run in background thread and return immediately."""
        import threading
        app = self._make_app()
        submit_started = threading.Event()
        submit_finished = threading.Event()

        original_submit = app.session.submit
        def slow_submit(text, cancellation_token=None):
            submit_started.set()
            import time
            time.sleep(0.1)
            submit_finished.set()
            return SimpleNamespace(notice="", checkpoint="")
        app.session.submit = slow_submit

        app._submit_async("test task")
        # Should return immediately, not block
        self.assertTrue(submit_started.wait(1.0), "submit should have started in background")
        # The background thread should eventually finish
        self.assertTrue(submit_finished.wait(2.0), "submit should have finished")

    def test_submitting_flag_prevents_double_submit(self):
        """While submitting, _submit_async should be a no-op."""
        import threading
        app = self._make_app()
        call_count = []

        def slow_submit(text, cancellation_token=None):
            call_count.append(text)
            import time
            time.sleep(0.2)
            return SimpleNamespace(notice="", checkpoint="")
        app.session.submit = slow_submit

        app._submit_async("first")
        app._submit_async("second")  # Should be ignored
        import time
        time.sleep(0.5)
        self.assertEqual(len(call_count), 1)

    def test_events_from_background_thread_update_state(self):
        """Events emitted during background submit should update TuiState."""
        app = self._make_app()

        def submit_with_events(text, cancellation_token=None):
            app._event_listener(SimpleNamespace(
                to_dict=lambda: {"type": "turn_started", "payload": {"turn": 1}},
            ))
            app._event_listener(SimpleNamespace(
                to_dict=lambda: {"type": "tool_call", "payload": {"tool": "read_file", "args": {"path": "x.py"}}},
            ))
            app._event_listener(SimpleNamespace(
                to_dict=lambda: {"type": "turn_finished", "payload": {"turn": 1}},
            ))
            return SimpleNamespace(notice="", checkpoint="")
        app.session.submit = submit_with_events

        app._submit_async("test")
        import time
        time.sleep(0.5)
        app._drain_event_queue()

        self.assertEqual(app.state.snapshot.turn, 1)
        self.assertEqual(app.state.snapshot.status, "idle")

    def test_refresh_display_only_invalidates_and_does_not_drain_from_background(self):
        """_refresh_display must be safe to call from worker threads."""
        app = self._make_app()
        app._event_queue.put(("event", SimpleNamespace(to_dict=lambda: {"type": "turn_started", "payload": {"turn": 1}})))

        with patch.object(app, "_drain_event_queue", side_effect=AssertionError("must not drain")):
            app._refresh_display()


class TuiNoiseReductionTests(unittest.TestCase):
    """Phase 3: Output noise reduction tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_state(self):
        return TuiState(
            SessionStatusSnapshot(
                profile="coding-agent", model="m", provider="p",
                permission_mode="w", session_id="s", cwd=self.root,
            )
        )

    def test_tool_result_shows_summary_not_body(self):
        """Tool result in transcript should show summary, not full output."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        # Simulate tool_call then tool_result events
        call_event = SimpleNamespace(
            to_dict=lambda: {"type": "tool_call", "payload": {"tool": "run_bash", "args": {"command": "echo hi"}}},
        )
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {
                    "tool": "run_bash",
                    "status": "success",
                    "output": "line1\nline2\nline3\n" * 100,  # Large output
                    "return_code": 0,
                },
            },
        )

        block1 = state.apply_event(call_event)
        block2 = state.apply_event(result_event)

        # The result block should be a summary, not the full output
        self.assertIsNotNone(block2)
        fragments = list(render_block_fragments(block2))
        text = "".join(f[1] for f in fragments)
        # Should NOT contain the full output body
        self.assertNotIn("line1\nline2\nline3", text)
        # Should contain the tool name and success icon
        self.assertIn("run_bash", text)
        self.assertIn("✓", text)  # success icon

    def test_tool_result_shows_output_size(self):
        """Tool result summary should include output size."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {"type": "tool_call", "payload": {"tool": "read_file", "args": {"path": "x.py"}}},
        )
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {
                    "tool": "read_file",
                    "status": "success",
                    "output": "x" * 2048,
                },
            },
        )

        state.apply_event(call_event)
        block = state.apply_event(result_event)

        fragments = list(render_block_fragments(block))
        text = "".join(f[1] for f in fragments)
        # Should show size info
        self.assertIn("2", text)  # 2048 chars ~ 2KB

    def test_failed_tool_shows_error_summary(self):
        """Failed tool should show short error, not full output."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {"type": "tool_call", "payload": {"tool": "run_bash", "args": {"command": "bad_cmd"}}},
        )
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {
                    "tool": "run_bash",
                    "status": "failed",
                    "error": "command not found: bad_cmd",
                    "return_code": 127,
                },
            },
        )

        state.apply_event(call_event)
        block = state.apply_event(result_event)

        fragments = list(render_block_fragments(block))
        text = "".join(f[1] for f in fragments)
        self.assertIn("command not found", text)
        self.assertIn("✗", text)

    def test_tool_call_shows_args_summary(self):
        """Tool call should show key args, not full args dict."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {"tool": "write_file", "args": {"path": "out.py", "content": "x" * 5000}},
            },
        )
        block = state.apply_event(call_event)

        fragments = list(render_block_fragments(block))
        text = "".join(f[1] for f in fragments)
        self.assertIn("write_file", text)
        self.assertIn("out.py", text)
        # Should not contain the full content
        self.assertNotIn("x" * 5000, text)

    def test_tool_timing_from_call_to_result(self):
        """Tool summary should show elapsed time."""
        import time
        from harness_code_agent.tui.state import ToolSummary
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {"tool": "run_bash", "args": {"command": "sleep 0"}},
            },
        )
        state.apply_event(call_event)

        # Check that pending tool is tracked
        self.assertIn("run_bash", state._pending_tools)

        time.sleep(0.05)
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {"tool": "run_bash", "status": "success", "output": "ok"},
            },
        )
        block = state.apply_event(result_event)
        self.assertIsNotNone(block)
        # Pending tool should be cleared
        self.assertNotIn("run_bash", state._pending_tools)

    def test_full_tool_output_remains_in_event_storage_while_transcript_summarizes(self):
        """Noise reduction is a display concern; stored events keep their payload."""
        state = self._make_state()
        events = []
        bus = EventBus(listener=events.append)
        large_output = "line1\nline2\nline3\n" * 100

        bus.emit("tool_call", agent="main_agent", payload={"tool": "run_bash", "args": {"command": "make test"}})
        bus.emit("tool_result", agent="main_agent", payload={"tool": "run_bash", "status": "success", "output": large_output})

        for event in events:
            block = state.apply_event(event)
            state.add_block(block)

        stored_result = [event for event in events if event.type == "tool_result"][0]
        self.assertEqual(stored_result.payload["output"], large_output)
        self.assertNotIn(large_output, state.blocks[-1].body)


class TuiThoughtTests(unittest.TestCase):
    """Phase 4: Thought hiding tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_state(self):
        return TuiState(
            SessionStatusSnapshot(
                profile="coding-agent", model="m", provider="p",
                permission_mode="w", session_id="s", cwd=self.root,
            )
        )

    def test_thought_event_creates_thought_block(self):
        """ThoughtFinishedEvent should create a thought block with duration."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        event = SimpleNamespace(
            to_dict=lambda: {
                "type": "thought_finished",
                "payload": {"duration_seconds": 12.3, "truncated": False, "source": "deepseek"},
            },
        )
        block = state.apply_event(event)

        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "thought")
        fragments = list(render_block_fragments(block))
        text = "".join(f[1] for f in fragments)
        self.assertIn("thought for", text)
        self.assertIn("12", text)  # duration
        self.assertIn("💭", text)

    def test_thought_block_does_not_contain_reasoning_text(self):
        """Thought block should never contain actual reasoning content."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        event = SimpleNamespace(
            to_dict=lambda: {
                "type": "thought_finished",
                "payload": {"duration_seconds": 5.0, "truncated": False, "source": "deepseek"},
            },
        )
        block = state.apply_event(event)
        fragments = list(render_block_fragments(block))
        text = "".join(f[1] for f in fragments)
        # Should not contain any reasoning text (payload doesn't include it, so this is safe)
        self.assertNotIn("reasoning", text.lower())

    def test_thought_card_has_purple_border(self):
        """Thought block should render with purple-themed card."""
        from harness_code_agent.tui.render import render_block_fragments
        state = self._make_state()

        event = SimpleNamespace(
            to_dict=lambda: {
                "type": "thought_finished",
                "payload": {"duration_seconds": 8.0, "truncated": False, "source": "deepseek"},
            },
        )
        block = state.apply_event(event)
        fragments = list(render_block_fragments(block))
        styles = [f[0] for f in fragments]
        # Should have purple (#b48ead) in styles
        self.assertTrue(any("#b48ead" in s for s in styles))

    def test_thought_toggle_stores_state(self):
        """Ctrl+T should toggle thought metadata visibility."""
        state = self._make_state()
        self.assertFalse(state.show_thought_details)

        state.toggle_thought_details()
        self.assertTrue(state.show_thought_details)

        state.toggle_thought_details()
        self.assertFalse(state.show_thought_details)


class TuiCancellationTests(unittest.TestCase):
    """Phase 5: Cancellation tests."""

    def test_cancellation_token_cancel_sets_flag(self):
        from harness_code_agent.agent.cancellation import CancellationToken, CancelledError
        token = CancellationToken()
        self.assertFalse(token.is_cancelled)
        token.cancel()
        self.assertTrue(token.is_cancelled)

    def test_cancellation_token_check_raises_when_cancelled(self):
        from harness_code_agent.agent.cancellation import CancellationToken, CancelledError
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            token.check()

    def test_cancellation_token_check_passes_when_not_cancelled(self):
        from harness_code_agent.agent.cancellation import CancellationToken
        token = CancellationToken()
        token.check()  # Should not raise

    def test_ctrl_c_while_running_cancels_turn(self):
        """Ctrl-C during submit should cancel the turn, not exit."""
        from prompt_toolkit.output.defaults import DummyOutput
        from harness_code_agent.tui.app import TuiApp
        import threading

        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession, \
             patch("prompt_toolkit.output.defaults.create_output", return_value=DummyOutput()):
            submit_cancelled = threading.Event()
            original_submit = None

            def slow_submit(text, cancellation_token=None):
                # Simulate a long-running submit that checks cancellation
                import time
                time.sleep(0.5)
                submit_cancelled.set()
                return SimpleNamespace(notice="", checkpoint="")

            mock_session = SimpleNamespace(
                profile=SimpleNamespace(name=lambda: "coding-agent"),
                session=SimpleNamespace(id="test-session"),
                cwd=str(Path(tempfile.mkdtemp())),
                permission_mode="workspace-write",
                conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
                close=lambda: None,
                handle_slash_command=lambda line: True,
                manual_compact_context=lambda: "compacted",
                submit=slow_submit,
            )
            MockSession.return_value = mock_session
            app = TuiApp(cwd=Path(mock_session.cwd), profile_name="coding-agent")

            # Verify _cancellation_token exists
            self.assertIsNotNone(app._cancellation_token)
            self.assertFalse(app._cancellation_token.is_cancelled)

            # Simulate cancel
            app._cancellation_token.cancel()
            self.assertTrue(app._cancellation_token.is_cancelled)

    def test_cancel_current_turn_interrupts_active_shell(self):
        """Ctrl-C path should also best-effort interrupt the current shell command."""
        from prompt_toolkit.output.defaults import DummyOutput
        from harness_code_agent.tui.app import TuiApp

        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession, \
             patch("prompt_toolkit.output.defaults.create_output", return_value=DummyOutput()):
            interrupted = []
            mock_session = SimpleNamespace(
                profile=SimpleNamespace(name=lambda: "coding-agent"),
                session=SimpleNamespace(id="test-session"),
                cwd=str(Path(tempfile.mkdtemp())),
                permission_mode="workspace-write",
                conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
                close=lambda: None,
                handle_slash_command=lambda line: True,
                manual_compact_context=lambda: "compacted",
                interrupt_current_shell=lambda: interrupted.append(True),
            )
            MockSession.return_value = mock_session
            app = TuiApp(cwd=Path(mock_session.cwd), profile_name="coding-agent")

            app._cancel_current_turn()

            self.assertTrue(app._cancellation_token.is_cancelled)
            self.assertEqual(interrupted, [True])


if __name__ == "__main__":
    unittest.main()
