"""Textual headless interactive tests using Pilot."""
import asyncio
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness_code_agent.agent.cancellation import CancelledError
from harness_code_agent.tui.app import TuiApp
from harness_code_agent.tui.state import SessionStatusSnapshot, TranscriptBlock, TuiState
from harness_code_agent.tui.widgets import SubmitTextArea


def _mock_session(root: Path):
    """Create a mock InteractiveSession."""
    from harness_code_agent.sessions.store import SessionStore

    store = SessionStore(root / ".harness")
    session_record = store.create(
        profile="coding-agent",
        cwd=root,
        model="model-a",
        permission_mode="workspace-write",
    )
    store.event_bus(session_record).emit(
        "llm_usage",
        payload={"prompt_tokens": 100, "cached_tokens": 80, "total_tokens": 120},
    )
    return SimpleNamespace(
        profile=SimpleNamespace(name=lambda: "coding-agent"),
        session=SimpleNamespace(id=session_record.id),
        session_store=store,
        cwd=str(root),
        permission_mode="workspace-write",
        conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
        close=lambda: None,
        handle_slash_command=lambda line: True,
        submit=lambda text, cancellation_token=None: SimpleNamespace(notice="", checkpoint=""),
        interrupt_current_shell=lambda: None,
        toggle_permission_mode=lambda: "permission mode switched: workspace-write -> llm-auto",
    )


def _mock_pending_session(root: Path):
    from harness_code_agent.sessions.store import SessionStore

    return SimpleNamespace(
        is_bound=False,
        profile=SimpleNamespace(name=lambda: "coding-agent"),
        session=None,
        session_id=None,
        display_profile="pending",
        session_store=SessionStore(root / ".harness"),
        cwd=str(root),
        permission_mode="workspace-write",
        conversation=None,
        close=lambda: None,
        handle_slash_command=lambda line: True,
        submit=lambda text, cancellation_token=None: SimpleNamespace(notice="", checkpoint=""),
        interrupt_current_shell=lambda: None,
        toggle_permission_mode=lambda: "permission mode switched: workspace-write -> llm-auto",
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
                self.assertIsNotNone(app.query_one("#input-text"))
                self.assertIsNotNone(app.query_one("#cmd-palette"))
        _run(_test())

    def test_narrow_layout_hides_empty_plan_and_input_line_numbers(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                transcript = app.query_one("#transcript")
                input_text = app.query_one("#input-text", SubmitTextArea)
                input_prompt = app.query_one("#input-prompt")

                self.assertGreaterEqual(transcript.region.width, 76)
                self.assertFalse(input_text.show_line_numbers)
                self.assertEqual(input_text.region.height, 3)
                self.assertEqual(input_prompt.region.height, 3)
                self.assertEqual(input_prompt.content_region.y, input_text.cursor_screen_offset.y)
                self.assertIn("Ask anything", input_text.render_line(0).text)

        _run(_test())

    def test_composer_clears_hint_and_grows_only_for_real_newlines(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(80, 24)) as pilot:
                text_area = app.query_one("#input-text", SubmitTextArea)
                await pilot.click("#input-text")
                await pilot.press("h")
                await pilot.pause()

                self.assertEqual(text_area.region.height, 3)
                self.assertNotIn("Ask anything", text_area.render_line(0).text)

                await pilot.press("shift+enter")
                await pilot.pause()
                self.assertEqual(text_area.region.height, 3)
                self.assertEqual(app.query_one("#input-prompt").region.height, 3)

        _run(_test())

    def test_wide_layout_renders_full_plan_update_in_transcript(self):
        async def _test():
            app = self._make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                from harness_code_agent.tui.app import SessionEvent
                event = SimpleNamespace(
                    to_dict=lambda: {
                        "type": "tool_result",
                        "payload": {
                            "tool": "update_plan_state",
                            "status": "success",
                            "metadata": {
                                "planning_state": {
                                    "steps": ["inspect", "fix", "verify"],
                                    "current_step": "fix",
                                    "completed_steps": ["inspect"],
                                }
                            },
                        },
                    }
                )
                app.post_message(SessionEvent(event))
                await pilot.pause()
                self.assertGreaterEqual(app.query_one("#transcript").region.width, 116)
                plan_blocks = [block for block in app.state.blocks if block.kind == "plan"]
                self.assertEqual(len(plan_blocks), 1)
                self.assertIn("inspect", plan_blocks[0].body)
                self.assertIn("fix", plan_blocks[0].body)
                self.assertIn("verify", plan_blocks[0].body)

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

    # ── Typing and input ────────────────────────────────────────────────────

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
                    await pilot.pause(0.01)
                    self.assertIn("hello", submitted)
                    text_area = app.query_one("#input-text", SubmitTextArea)
                    self.assertEqual(text_area.text, "")
        _run(_test())

    def test_input_stays_visible_and_queues_while_turn_runs(self):
        async def _test():
            submitted = []
            started = threading.Event()
            release = threading.Event()
            mock = _mock_session(self.root)

            def capture(text, cancellation_token=None):
                submitted.append(text)
                started.set()
                release.wait(2)
                return SimpleNamespace(notice="", checkpoint="")

            mock.submit = capture

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("f", "i", "r", "s", "t")
                    await pilot.press("enter")
                    for _ in range(20):
                        if started.is_set():
                            break
                        await pilot.pause(0.01)

                    input_area = app.query_one("#input-area")
                    self.assertTrue(input_area.display)
                    await pilot.press("s", "e", "c", "o", "n", "d")
                    await pilot.press("enter")
                    text_area = app.query_one("#input-text", SubmitTextArea)
                    self.assertEqual(text_area.text, "")
                    self.assertEqual(submitted, ["first"])

                    release.set()
                    for _ in range(50):
                        if submitted == ["first", "second"] and not app._submitting:
                            break
                        await pilot.pause(0.01)
                    self.assertEqual(submitted, ["first", "second"])
        _run(_test())

    def test_ctrl_c_cancels_active_turn_but_preserves_queue(self):
        async def _test():
            submitted = []
            started = threading.Event()
            interrupted = []
            mock = _mock_session(self.root)
            mock.interrupt_current_shell = lambda: interrupted.append(True) or True

            def capture(text, cancellation_token=None):
                submitted.append(text)
                if text == "first":
                    started.set()
                    for _ in range(200):
                        if cancellation_token is not None and cancellation_token.is_cancelled:
                            raise CancelledError("Turn cancelled by user")
                        time.sleep(0.01)
                    raise AssertionError("first turn was not cancelled")
                return SimpleNamespace(notice="", checkpoint="")

            mock.submit = capture

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    self.assertTrue(app._submit_async("first"))
                    for _ in range(20):
                        if started.is_set():
                            break
                        await pilot.pause(0.01)
                    self.assertTrue(app._submit_async("second"))

                    await pilot.press("ctrl+c")
                    for _ in range(50):
                        if submitted == ["first", "second"] and not app._submitting:
                            break
                        await pilot.pause(0.01)

                    self.assertEqual(submitted, ["first", "second"])
                    self.assertEqual(interrupted, [True])
                    self.assertTrue(
                        any(block.title == "turn cancelled" for block in app.state.blocks)
                    )
        _run(_test())

    def test_slash_command_queues_behind_running_turn(self):
        async def _test():
            submitted = []
            commands = []
            started = threading.Event()
            release = threading.Event()
            mock = _mock_session(self.root)
            mock.handle_slash_command = lambda line: commands.append(line) or True

            def capture(text, cancellation_token=None):
                submitted.append(text)
                started.set()
                release.wait(2)
                return SimpleNamespace(notice="", checkpoint="")

            mock.submit = capture

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    self.assertTrue(app._submit_async("first"))
                    for _ in range(20):
                        if started.is_set():
                            break
                        await pilot.pause(0.01)

                    self.assertTrue(app._submit_async("/help"))
                    self.assertEqual(commands, [])

                    release.set()
                    for _ in range(50):
                        if commands == ["/help"] and not app._submitting:
                            break
                        await pilot.pause(0.01)

                    self.assertEqual(submitted, ["first"])
                    self.assertEqual(commands, ["/help"])
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
                    await pilot.pause(0.01)
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
                    await pilot.pause(0.01)
                    self.assertIn("/help", commands)
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

    def test_ctrl_o_opens_observability_screen_with_mode_toggle_and_escape(self):
        async def _test():
            mock = _mock_session(self.root)
            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test(size=(120, 40)) as pilot:
                    from harness_code_agent.tui.screens import ObservabilityScreen

                    await pilot.press("ctrl+o")
                    await pilot.pause(0.01)
                    self.assertIsInstance(app.screen, ObservabilityScreen)
                    body = str(app.screen.query_one("#observability-body").renderable)
                    self.assertIn("Observability dashboard", body)
                    self.assertIn("cache hit ratio: 80.0%", body)

                    await pilot.press("tab")
                    await pilot.pause(0.01)
                    body = str(app.screen.query_one("#observability-body").renderable)
                    self.assertIn("Project observability", body)

                    await pilot.press("e")
                    await pilot.pause(0.01)
                    footer = str(app.screen.query_one("#observability-footer").renderable)
                    self.assertIn("observability_export_markdown:", footer)

                    await pilot.press("escape")
                    await pilot.pause(0.01)
                    self.assertNotIsInstance(app.screen, ObservabilityScreen)
        _run(_test())

    def test_ctrl_o_handles_pending_current_session(self):
        async def _test():
            mock = _mock_pending_session(self.root)
            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.press("ctrl+o")
                    await pilot.pause(0.01)

                    body = str(app.screen.query_one("#observability-body").renderable)
                    self.assertIn("No active session yet", body)

                    await pilot.press("e")
                    await pilot.pause(0.01)
                    footer = str(app.screen.query_one("#observability-footer").renderable)
                    self.assertIn("No active session yet", footer)
        _run(_test())

    # ── Submit behavior ─────────────────────────────────────────────────────

    def test_first_task_auto_submitted(self):
        async def _test():
            submitted = []
            mock = _mock_session(self.root)
            mock.submit = lambda text, **kw: submitted.append(text) or SimpleNamespace(notice="", checkpoint="")

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent", first_task="fix the bug")
                async with app.run_test() as pilot:
                    await pilot.pause(0.01)
                    self.assertIn("fix the bug", submitted)
        _run(_test())

    # ── Events and state ────────────────────────────────────────────────────

    def test_stream_delta_updates_transcript(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import StreamDelta
                app.post_message(StreamDelta("Hello "))
                await pilot.pause(0.01)
                app.post_message(StreamDelta("world"))
                await pilot.pause(0.01)
                transcript = app.query_one("#transcript")
                self.assertGreater(len(transcript.lines), 0)
        _run(_test())

    def test_bursty_stream_deltas_are_coalesced_before_redraw(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import StreamDelta

                with patch.object(app, "_redraw_transcript", wraps=app._redraw_transcript) as redraw:
                    for _ in range(40):
                        app.post_message(StreamDelta("x"))
                    await pilot.pause(0.05)

                    self.assertGreater(redraw.call_count, 0)
                    self.assertLess(redraw.call_count, 10)
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
                await pilot.pause(0.01)
                self.assertEqual(app.state.snapshot.turn, 1)
                self.assertEqual(app.state.snapshot.status, "running")
        _run(_test())

    def test_plan_update_event_contains_every_step(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import SessionEvent
                event = SimpleNamespace(
                    to_dict=lambda: {
                        "type": "tool_result",
                        "payload": {
                            "tool": "update_plan_state",
                            "status": "success",
                            "metadata": {
                                "planning_state": {
                                    "steps": ["write failing test", "implement panel", "run tests"],
                                    "current_step": "implement panel",
                                    "completed_steps": ["write failing test"],
                                }
                            },
                        },
                    },
                )
                app.post_message(SessionEvent(event))
                await pilot.pause(0.01)

                plan_blocks = [block for block in app.state.blocks if block.kind == "plan"]
                self.assertEqual(len(plan_blocks), 1)
                self.assertIn("write failing test", plan_blocks[0].body)
                self.assertIn("implement panel", plan_blocks[0].body)
                self.assertIn("run tests", plan_blocks[0].body)
        _run(_test())

    def test_output_appears_in_transcript(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                app._output("test output", title="test")
                await pilot.pause(0.01)
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
                await pilot.pause(0.01)
                status_bar = app.query_one("#status-bar")
                self.assertEqual(status_bar.turn, 5)
                self.assertEqual(status_bar.status, "running")
        _run(_test())

    def test_click_status_bar_toggles_permission_mode(self):
        async def _test():
            mock = _mock_session(self.root)

            def toggle_permission_mode():
                mock.permission_mode = "llm-auto"
                return "permission mode switched: workspace-write -> llm-auto"

            mock.toggle_permission_mode = toggle_permission_mode

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.click("#status-bar")
                    await pilot.pause(0.01)
                    self.assertEqual(app.state.snapshot.permission_mode, "llm-auto")
                    self.assertFalse(any(block.title == "permission mode switched" for block in app.state.blocks))
        _run(_test())

    def test_ctrl_p_no_longer_toggles_permission_mode(self):
        async def _test():
            mock = _mock_session(self.root)
            toggled = []

            def toggle_permission_mode():
                toggled.append(True)
                mock.permission_mode = "llm-auto"
                return "permission mode switched: workspace-write -> llm-auto"

            mock.toggle_permission_mode = toggle_permission_mode

            with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
                MockSession.return_value = mock
                app = TuiApp(cwd=self.root, profile_name="coding-agent")
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+p")
                    await pilot.pause(0.01)
                    self.assertEqual(toggled, [])
                    self.assertEqual(app.state.snapshot.permission_mode, "workspace-write")
        _run(_test())

    def test_stream_delta_is_visible_before_final_assistant_message(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import StreamDelta

                app.post_message(StreamDelta("Hello "))
                await pilot.pause(0.02)
                app.post_message(StreamDelta("world"))
                await pilot.pause(0.05)

                transcript = app.query_one("#transcript")
                rendered = "\n".join(str(line) for line in transcript.lines)
                self.assertIn("Hello", rendered)
                self.assertIn("world", rendered)
        _run(_test())

    def test_streamed_assistant_message_does_not_duplicate_streamed_block(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import SessionEvent, StreamDelta

                app.post_message(StreamDelta("Hello "))
                await pilot.pause(0.01)
                app.post_message(StreamDelta("world"))
                await pilot.pause(0.01)

                event = SimpleNamespace(
                    to_dict=lambda: {
                        "type": "assistant_message",
                        "payload": {"turn": 1, "text": "Hello world", "streamed": True},
                    },
                )
                app.post_message(SessionEvent(event))
                await pilot.pause(0.01)

                assistant_blocks = [
                    block
                    for block in app.state.blocks
                    if block.kind == "assistant" and block.body == "Hello world"
                ]
                self.assertEqual(len(assistant_blocks), 1)
        _run(_test())

    def test_submit_complete_before_assistant_event_does_not_duplicate_streamed_text(self):
        async def _test():
            app = self._make_app()
            async with app.run_test() as pilot:
                from harness_code_agent.tui.app import SessionEvent, StreamDelta, SubmitComplete

                app.post_message(StreamDelta("Hello world"))
                await pilot.pause(0.01)
                app.post_message(SubmitComplete(SimpleNamespace(notice="", checkpoint="")))
                await pilot.pause(0.01)
                event = SimpleNamespace(
                    to_dict=lambda: {
                        "type": "assistant_message",
                        "payload": {"turn": 1, "text": "Hello world"},
                    },
                )
                app.post_message(SessionEvent(event))
                await pilot.pause(0.01)

                assistant_blocks = [
                    block
                    for block in app.state.blocks
                    if block.kind == "assistant" and block.body == "Hello world"
                ]
                self.assertEqual(len(assistant_blocks), 1)
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
                await pilot.pause(0.01)

                input_area = app.query_one("#input-area")
                self.assertFalse(input_area.display)
                self.assertIsNotNone(app.query_one("#approval-panel"))

                # Double-press 1 to approve
                await pilot.press("1")
                await pilot.pause(0.01)
                await pilot.press("1")
                await pilot.pause(0.01)

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
                await pilot.pause(0.01)

                # Double-press 3 to deny
                await pilot.press("3")
                await pilot.pause(0.01)
                await pilot.press("3")
                await pilot.pause(0.01)

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
                await pilot.pause(0.01)

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
                await pilot.pause(0.01)
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
                await pilot.pause(0.01)

                # Enter should submit current selection (default: Approve)
                await pilot.press("enter")
                await pilot.pause(0.01)
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
                await pilot.pause(0.01)

                # Double-press 2 to select Vue
                await pilot.press("2")
                await pilot.pause(0.01)
                await pilot.press("2")
                await pilot.pause(0.01)

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
                await pilot.pause(0.01)
                await pilot.press("escape")
                await pilot.pause(0.01)
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
                await pilot.pause(0.01)

                panel = app.query_one("#question-panel")
                self.assertEqual(panel._selected_index, 0)
                await pilot.press("down")
                self.assertEqual(panel._selected_index, 1)
                await pilot.press("down")
                self.assertEqual(panel._selected_index, 2)
                await pilot.press("up")
                self.assertEqual(panel._selected_index, 1)

                await pilot.press("escape")
                await pilot.pause(0.01)
        _run(_test())


if __name__ == "__main__":
    unittest.main()
