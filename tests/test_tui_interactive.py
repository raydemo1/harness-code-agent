"""Textual headless interactive tests using Pilot."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness_code_agent.tui.app import TuiApp
from harness_code_agent.tui.state import SessionStatusSnapshot, TranscriptBlock, TuiState
from harness_code_agent.tui.widgets import CommandPalette, InputArea, SubmitTextArea


def _mock_session(root: Path):
    """Create a mock InteractiveSession."""
    return SimpleNamespace(
        profile=SimpleNamespace(name=lambda: "coding-agent"),
        session=SimpleNamespace(id="test-session-001"),
        cwd=str(root),
        permission_mode="workspace-write",
        conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
        close=lambda: None,
        handle_slash_command=lambda line: True,
        manual_compact_context=lambda: "compacted",
        submit=lambda text, cancellation_token=None: SimpleNamespace(notice="", checkpoint=""),
        interrupt_current_shell=lambda: None,
        toggle_permission_mode=lambda: "permission mode switched: workspace-write -> danger-full-access",
    )


def _run(coro):
    """Run an async test coroutine."""
    return asyncio.run(coro)


class TuiInteractiveTests(unittest.TestCase):
    """Headless interactive tests using Textual Pilot."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_app(self, **kwargs):
        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
            MockSession.return_value = _mock_session(self.root)
            app = TuiApp(cwd=self.root, profile_name="coding-agent", **kwargs)
        return app

    # ── Widget mounting ─────────────────────────────────────────────────────

    def test_app_mounts_all_widgets(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                self.assertIsNotNone(app.query_one("#transcript"))
                self.assertIsNotNone(app.query_one("#input-area"))
                self.assertIsNotNone(app.query_one("#status-bar"))
                self.assertIsNotNone(app.query_one("#context-bar"))
                self.assertIsNotNone(app.query_one("#input-text"))
                self.assertIsNotNone(app.query_one("#cmd-palette"))
        _run(_test())

    def test_tui_approval_provider_has_project_allowlist(self):
        async def _test():
            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = _mock_session(self.root)
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    approval_provider = MockSession.call_args.kwargs["approval_provider"]
                    self.assertIsNotNone(approval_provider.allowlist)
                    self.assertEqual(approval_provider.allowlist.project_root, self.root.resolve())
        _run(_test())

    def test_transcript_shows_welcome_message(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                transcript = app.query_one("#transcript")
                self.assertGreater(len(transcript.lines), 0)
        _run(_test())

    def test_input_area_receives_focus(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                text_area = app.query_one("#input-text", SubmitTextArea)
                self.assertTrue(text_area.has_focus)
        _run(_test())

    # ── Typing and input ────────────────────────────────────────────────────

    def test_typing_in_input_area(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                text_area = app.query_one("#input-text", SubmitTextArea)
                await pilot.press("h", "e", "l", "l", "o")
                self.assertEqual(text_area.text, "hello")
        _run(_test())

    def test_shift_enter_inserts_newline(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                text_area = app.query_one("#input-text", SubmitTextArea)
                await pilot.press("l", "i", "n", "e", "1")
                await pilot.press("shift+enter")
                await pilot.press("l", "i", "n", "e", "2")
                self.assertIn("\n", text_area.text)
                self.assertIn("line1", text_area.text)
                self.assertIn("line2", text_area.text)
        _run(_test())

    def test_enter_submits_text(self):
        async def _test():
            submitted = []
            mock = _mock_session(self.root)
            orig_submit = mock.submit
            def capture(text, cancellation_token=None):
                submitted.append(text)
                return SimpleNamespace(notice="", checkpoint="")
            mock.submit = capture

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("h", "e", "l", "l", "o")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIn("hello", submitted)
                    text_area = app.query_one("#input-text", SubmitTextArea)
                    self.assertEqual(text_area.text, "")
        _run(_test())

    def test_empty_input_not_submitted(self):
        async def _test():
            submitted = []
            mock = _mock_session(self.root)
            mock.submit = lambda text, **kw: submitted.append(text) or SimpleNamespace(notice="", checkpoint="")

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(len(submitted), 0)
        _run(_test())

    # ── Slash commands ──────────────────────────────────────────────────────

    def test_slash_command_dispatched(self):
        async def _test():
            commands = []
            mock = _mock_session(self.root)
            mock.handle_slash_command = lambda line: commands.append(line) or True

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("slash")
                    await pilot.press("h", "e", "l", "p")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIn("/help", commands)
        _run(_test())

    def test_slash_command_palette_appears(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                palette = app.query_one("#cmd-palette", CommandPalette)
                await pilot.press("slash")
                await pilot.pause()
                self.assertTrue(palette.display)
                self.assertGreater(len(palette.candidates), 0)
        _run(_test())

    def test_slash_command_palette_navigates(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.pause()
                palette = app.query_one("#cmd-palette", CommandPalette)
                initial = palette.selected_index
                await pilot.press("down")
                self.assertEqual(palette.selected_index, (initial + 1) % len(palette.candidates))
                await pilot.press("up")
                self.assertEqual(palette.selected_index, initial)
        _run(_test())

    def test_tab_selects_palette_candidate(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                await pilot.press("slash")
                await pilot.pause()
                palette = app.query_one("#cmd-palette", CommandPalette)
                first_candidate = palette.candidates[0][0] if palette.candidates else None
                if first_candidate:
                    await pilot.press("tab")
                    await pilot.pause()
                    text_area = app.query_one("#input-text", SubmitTextArea)
                    self.assertIn(first_candidate, text_area.text)
        _run(_test())

    def test_escape_closes_palette(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                palette = app.query_one("#cmd-palette", CommandPalette)
                await pilot.press("slash")
                await pilot.pause()
                self.assertTrue(palette.display)
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(palette.display)
        _run(_test())

    # ── Keyboard shortcuts ──────────────────────────────────────────────────

    def test_ctrl_c_cancels_turn(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                app._submitting = True
                await pilot.press("ctrl+c")
                self.assertTrue(app._cancellation_token.is_cancelled)
        _run(_test())

    def test_ctrl_t_toggles_thought(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                self.assertFalse(app.state.show_thought_details)
                await pilot.press("ctrl+t")
                self.assertTrue(app.state.show_thought_details)
                await pilot.press("ctrl+t")
                self.assertFalse(app.state.show_thought_details)
        _run(_test())

    # ── Submit behavior ─────────────────────────────────────────────────────

    def test_double_submit_prevented(self):
        async def _test():
            call_count = []
            import time
            mock = _mock_session(self.root)
            def slow_submit(text, cancellation_token=None):
                call_count.append(text)
                time.sleep(0.3)
                return SimpleNamespace(notice="", checkpoint="")
            mock.submit = slow_submit

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("t", "a", "s", "k", "1")
                    await pilot.press("enter")
                    await pilot.pause()
                    await pilot.press("t", "a", "s", "k", "2")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(len(call_count), 1)
        _run(_test())

    def test_first_task_auto_submitted(self):
        async def _test():
            submitted = []
            mock = _mock_session(self.root)
            mock.submit = lambda text, **kw: submitted.append(text) or SimpleNamespace(notice="", checkpoint="")

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent", first_task="fix the bug")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    self.assertIn("fix the bug", submitted)
        _run(_test())

    # ── Events and state ────────────────────────────────────────────────────

    def test_stream_delta_updates_transcript(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import StreamDelta
                app.post_message(StreamDelta("Hello "))
                await pilot.pause()
                app.post_message(StreamDelta("world"))
                await pilot.pause()
                transcript = app.query_one("#transcript")
                self.assertGreater(len(transcript.lines), 0)
        _run(_test())

    def test_session_event_updates_state(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import SessionEvent
                event = SimpleNamespace(
                    to_dict=lambda: {"type": "turn_started", "payload": {"turn": 1}},
                )
                app.post_message(SessionEvent(event))
                await pilot.pause()
                self.assertEqual(app.state.snapshot.turn, 1)
                self.assertEqual(app.state.snapshot.status, "running")
        _run(_test())

    def test_output_appears_in_transcript(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                app._output("test output", title="test")
                await pilot.pause()
                self.assertTrue(any(b.title == "test" and b.body == "test output" for b in app.state.blocks))
        _run(_test())

    def test_status_bar_updates_from_events(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import SessionEvent
                event = SimpleNamespace(
                    to_dict=lambda: {"type": "turn_started", "payload": {"turn": 5}},
                )
                app.post_message(SessionEvent(event))
                await pilot.pause()
                status_bar = app.query_one("#status-bar")
                self.assertEqual(status_bar.turn, 5)
                self.assertEqual(status_bar.status, "running")
        _run(_test())

    def test_context_bar_shows_percentage(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                # Manually set context tokens
                app.state.snapshot.context_tokens = 70000
                app.state.snapshot.context_window_tokens = 100000
                app.query_one("#context-bar").update_from_snapshot(app.state.snapshot)
                await pilot.pause()
                ctx_bar = app.query_one("#context-bar")
                self.assertEqual(ctx_bar.context_percent, 70)
        _run(_test())

    def test_context_bar_shows_token_count_and_shortcuts(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                app.state.snapshot.context_tokens = 70000
                app.state.snapshot.context_window_tokens = 100000
                app.state.snapshot.permission_mode = "workspace-write"
                app.query_one("#context-bar").update_from_snapshot(app.state.snapshot)
                await pilot.pause()
                plain = app.query_one("#context-bar").render().plain
                self.assertIn("70K/100K", plain)
                self.assertIn("Ctrl+K", plain)
                self.assertIn("Ctrl+P", plain)
        _run(_test())

    def test_ctrl_p_toggles_permission_mode(self):
        async def _test():
            mock = _mock_session(self.root)

            def toggle_permission_mode():
                mock.permission_mode = "danger-full-access"
                return "permission mode switched: workspace-write -> danger-full-access"

            mock.toggle_permission_mode = toggle_permission_mode

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    self.assertEqual(app.state.snapshot.permission_mode, "danger-full-access")
                    self.assertTrue(any(block.title == "permission mode switched" for block in app.state.blocks))
        _run(_test())

    def test_ctrl_k_compacts_context_and_reports_result(self):
        async def _test():
            mock = _mock_session(self.root)
            calls = []

            def compact():
                calls.append(True)
                return "compacted old context"

            mock.manual_compact_context = compact

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+k")
                    await pilot.pause()
                    self.assertEqual(calls, [True])
                    self.assertTrue(any(block.title == "context compacted" for block in app.state.blocks))
        _run(_test())

    # ── Approval panel ──────────────────────────────────────────────────────

    def test_approval_panel_replaces_input(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.approvals import ApprovalRequest

                event = threading.Event()
                holder = [None]
                request = ApprovalRequest(
                    tool_name="run_bash",
                    args={"command": "echo hi"},
                    risk="low",
                    reason="test",
                )
                app.show_approval_panel(request, event, holder)
                await pilot.pause()

                input_area = app.query_one("#input-area")
                self.assertFalse(input_area.display)
                self.assertIsNotNone(app.query_one("#approval-panel"))

                # Double-press 1 to approve
                await pilot.press("1")
                await pilot.pause()
                await pilot.press("1")
                await pilot.pause()

                self.assertTrue(input_area.display)
                self.assertTrue(holder[0])
        _run(_test())

    def test_approval_panel_deny(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.approvals import ApprovalRequest

                event = threading.Event()
                holder = [None]
                request = ApprovalRequest(
                    tool_name="run_bash",
                    args={"command": "rm -rf /"},
                    risk="high",
                    reason="dangerous",
                )
                app.show_approval_panel(request, event, holder)
                await pilot.pause()

                # Double-press 3 to deny
                await pilot.press("3")
                await pilot.pause()
                await pilot.press("3")
                await pilot.pause()

                self.assertFalse(holder[0])
        _run(_test())

    def test_approval_panel_arrow_navigation(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.approvals import ApprovalRequest

                event = threading.Event()
                holder = [None]
                request = ApprovalRequest(
                    tool_name="run_bash",
                    args={"command": "echo hi"},
                    risk="low",
                    reason="test",
                )
                app.show_approval_panel(request, event, holder)
                await pilot.pause()

                panel = app.query_one("#approval-panel")
                self.assertEqual(panel._selected_index, 0)
                await pilot.press("right")
                self.assertEqual(panel._selected_index, 1)
                await pilot.press("right")
                self.assertEqual(panel._selected_index, 2)
                await pilot.press("left")
                self.assertEqual(panel._selected_index, 1)

                # Cleanup
                await pilot.press("escape")
                await pilot.pause()
        _run(_test())

    def test_approval_panel_enter_submits(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.approvals import ApprovalRequest

                event = threading.Event()
                holder = [None]
                request = ApprovalRequest(
                    tool_name="run_bash",
                    args={"command": "echo hi"},
                    risk="low",
                    reason="test",
                )
                app.show_approval_panel(request, event, holder)
                await pilot.pause()

                # Enter should submit current selection (default: Approve)
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(holder[0])
        _run(_test())

    # ── Question panel ──────────────────────────────────────────────────────

    def test_question_panel_number_selection(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

                event = threading.Event()
                holder = [None]
                request = QuestionRequest(
                    question="Which framework?",
                    options=[
                        QuestionOption(label="React"),
                        QuestionOption(label="Vue"),
                        QuestionOption(label="Svelte"),
                    ],
                )
                app.show_question_panel(request, event, holder)
                await pilot.pause()

                # Double-press 2 to select Vue
                await pilot.press("2")
                await pilot.pause()
                await pilot.press("2")
                await pilot.pause()

                self.assertIsNotNone(holder[0])
                self.assertEqual(holder[0]["label"], "Vue")
        _run(_test())

    def test_question_panel_escape_cancels(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

                event = threading.Event()
                holder = [None]
                request = QuestionRequest(
                    question="Which?",
                    options=[QuestionOption(label="A"), QuestionOption(label="B")],
                )
                app.show_question_panel(request, event, holder)
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsNone(holder[0])
        _run(_test())

    def test_question_panel_arrow_navigation(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                import threading
                from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest

                event = threading.Event()
                holder = [None]
                request = QuestionRequest(
                    question="Pick one",
                    options=[
                        QuestionOption(label="A"),
                        QuestionOption(label="B"),
                        QuestionOption(label="C"),
                    ],
                )
                app.show_question_panel(request, event, holder)
                await pilot.pause()

                panel = app.query_one("#question-panel")
                self.assertEqual(panel._selected_index, 0)
                await pilot.press("down")
                self.assertEqual(panel._selected_index, 1)
                await pilot.press("down")
                self.assertEqual(panel._selected_index, 2)
                await pilot.press("up")
                self.assertEqual(panel._selected_index, 1)

                await pilot.press("escape")
                await pilot.pause()
        _run(_test())


if __name__ == "__main__":
    unittest.main()
