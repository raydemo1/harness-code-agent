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
    TuiApprovalProvider,
    _derive_persistent_prefix,
)
from harness_code_agent.tui.question import TuiQuestionProvider
from harness_code_agent.tui.commands import default_command_registry
from harness_code_agent.tui.completion import current_mention_query, mention_candidates, replace_mention_fragment
from harness_code_agent.tui.state import PlanStep, SessionStatusSnapshot, TranscriptBlock, TuiState
from harness_code_agent.tui.widgets import block_to_rich, ContextBar, PlanPanel, StatusBar


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
        review_result = registry.execute("/review", session)

        self.assertIn("Profiles:", help_text)
        self.assertIn("/review", help_text)
        self.assertIn("/checkpoint", help_text)
        self.assertEqual(result.text, "switched coding-agent")
        self.assertEqual(review_result.text, "switched review")
        self.assertEqual(calls, ["coding-agent", "review"])

    def test_command_registry_reports_validation_errors(self):
        registry = default_command_registry()
        result = registry.execute("/config nope", SimpleNamespace())

        self.assertIn("Usage: /config show", result.text)
        self.assertTrue(result.should_continue)

    def test_resume_command_can_run_before_session_is_bound(self):
        registry = default_command_registry()
        calls = []
        session = SimpleNamespace(
            is_bound=False,
            _inject_resume_context=lambda session_id: calls.append(session_id) or f"queued {session_id}",
        )

        result = registry.execute("/resume previous-session", session)

        self.assertEqual(result.text, "queued previous-session")
        self.assertEqual(calls, ["previous-session"])
        self.assertTrue(result.should_continue)

    def test_observe_command_formats_and_exports_current_session(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session_record = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        store.event_bus(session_record).emit(
            "llm_usage",
            payload={"prompt_tokens": 100, "cached_tokens": 80, "total_tokens": 120},
        )
        session = SimpleNamespace(
            session_store=store,
            session=SimpleNamespace(id=session_record.id),
        )
        registry = default_command_registry()

        report = registry.execute("/observe current", session)
        exported = registry.execute("/observe export current", session)

        self.assertIn("Observability dashboard", report.text)
        self.assertIn("cache hit ratio: 80.0%", report.text)
        self.assertIn("observability_export_markdown:", exported.text)
        self.assertIn("observability_export_json:", exported.text)

    def test_observe_command_formats_project_overview(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session_record = store.create(
            profile="coding-agent",
            cwd=self.temp_dir,
            model="model-a",
            permission_mode="workspace-write",
        )
        store.event_bus(session_record).emit(
            "llm_usage",
            payload={"prompt_tokens": 100, "cached_tokens": 20, "total_tokens": 120},
        )
        registry = default_command_registry()
        session = SimpleNamespace(session_store=store, session=SimpleNamespace(id=session_record.id))

        report = registry.execute("/observe project", session)

        self.assertIn("Project observability", report.text)
        self.assertIn("sessions: 1", report.text)

    def test_observability_screen_handles_pending_current_session(self):
        from harness_code_agent.tui.screens import ObservabilityScreen

        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = SimpleNamespace(
            is_bound=False,
            session=None,
            session_store=store,
        )
        screen = ObservabilityScreen(session)

        self.assertIsNone(screen._current_session_id())
        self.assertIn("No active session yet", screen._current_session_body())

    def test_explicit_quoted_file_mention_resolves_paths_with_spaces(self):
        Path(self.temp_dir, "space name.md").write_text("hello space\n", encoding="utf-8")
        store = SessionStore(Path(self.temp_dir) / ".harness")

        mentions = parse_mentions('read @file:"space name.md" now')
        resolved = resolve_mentions(
            'read @file:"space name.md" now',
            workspace_root=self.temp_dir,
            session_store=store,
        )

        self.assertEqual(mentions[0].target, "space name.md")
        self.assertEqual(resolved[0].kind, "file")
        self.assertIn("space name.md", resolved[0].content)
        self.assertIn("Use read_file to inspect this file if needed.", resolved[0].content)
        self.assertNotIn("hello space", resolved[0].content)

    def test_mention_candidates_fuzzy_files_sessions_and_exclusions(self):
        Path(self.temp_dir, "README.md").write_text("read me\n", encoding="utf-8")
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
        readme_candidates = mention_candidates(self.root, "rea", store)
        session_candidates = mention_candidates(self.root, "session:" + session.id[:8], store)
        skill_candidates = mention_candidates(
            self.root,
            "skill:dia",
            store,
            skill_catalog=[
                {
                    "name": "diagnose",
                    "description": "Debug hard failures.",
                    "path": "skills/diagnose/SKILL.md",
                }
            ],
        )

        self.assertIn('file:"space name.md"', [item.insert_text for item in file_candidates])
        self.assertIn("file:README.md", [item.insert_text for item in readme_candidates])
        self.assertNotIn("node_modules/ignored.js", [item.insert_text for item in file_candidates])
        self.assertIn("session:" + session.id, [item.insert_text for item in session_candidates])
        self.assertIn("skill:diagnose", [item.insert_text for item in skill_candidates])

    def test_current_mention_query_handles_plain_and_quoted_mentions(self):
        self.assertEqual(current_mention_query("fix @READ"), ("READ", -5))
        self.assertEqual(current_mention_query("fix @file:READ"), ("file:READ", -10))
        self.assertIsNone(current_mention_query("email@domain"))

    def test_replace_mention_fragment_preserves_surrounding_text(self):
        self.assertEqual(
            replace_mention_fragment("please inspect @rea before editing", "file:README.md"),
            "please inspect @file:README.md before editing",
        )
        self.assertEqual(
            replace_mention_fragment("use @skill:dia", "skill:diagnose"),
            "use @skill:diagnose ",
        )

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
        self.assertGreaterEqual(len(state.blocks), 4)

    def test_fallback_events_update_tui_attention_state(self):
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

        warning = state.apply_event({
            "type": "agent_budget_warning",
            "payload": {"limit_type": "total_tokens", "used": 80, "limit": 100},
        })
        fallback = state.apply_event({
            "type": "agent_fallback",
            "payload": {"reason": "loop_detected", "limit_type": "tool_fingerprint"},
        })

        self.assertEqual(warning.kind, "status")
        self.assertEqual(warning.status, "warning")
        self.assertEqual(fallback.kind, "failure")
        self.assertEqual(fallback.status, "blocked")
        self.assertEqual(state.snapshot.status, "blocked")

    def test_update_plan_state_result_updates_plan_steps(self):
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

        state.apply_event(
            {
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
            }
        )

        self.assertEqual(
            state.plan_steps,
            [
                PlanStep("write failing test", "completed"),
                PlanStep("implement panel", "current"),
                PlanStep("run tests", "pending"),
            ],
        )

    def test_approval_events_update_state_without_transcript_block(self):
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

        self.assertIsNone(block)
        self.assertEqual(state.pending_approval["tool"], "run_bash")

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

        rel_paths = [p[0] for p in paths]
        self.assertIn("src", rel_paths)
        self.assertIn("src/app.js", rel_paths)
        self.assertNotIn("node_modules/hidden.js", rel_paths)
        node_modules_resolved = node_modules_dir.resolve()
        self.assertNotIn(node_modules_resolved, scanned_dirs)


class TuiApprovalTests(unittest.TestCase):
    """Tests for the Textual-based approval provider."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_approval_allowlist_persists_prefix_rule_and_reuses_it(self):
        """Allowlist should persist prefix rules and auto-approve matching commands."""
        allowlist = ApprovalAllowlist(self.root)
        request1 = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "npm run test -- --watch"},
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )
        request2 = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "npm run test -- tests/test_tui.py"},
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )

        # First call: no match, need to persist
        provider = TuiApprovalProvider(project_root=self.root)
        prefix = _derive_persistent_prefix(str(request1.args.get("command", "")))
        self.assertEqual(prefix, ["npm", "run", "test"])

        # Manually persist
        allowlist.add_prefix_rule(prefix, command=str(request1.args.get("command", "")))

        # Second call should auto-match
        result = provider.request(request2)
        self.assertTrue(result.approved)
        self.assertIn("allowlist", result.reason.lower())

    def test_approval_allowlist_no_match_returns_none(self):
        """Allowlist should return None for non-matching commands."""
        allowlist = ApprovalAllowlist(self.root)
        allowlist.add_prefix_rule(["npm", "run", "test"], command="npm run test -- --watch")

        self.assertIsNone(allowlist.match("npm run build"))
        self.assertIsNotNone(allowlist.match("npm run test -- foo"))


class TuiNoiseReductionTests(unittest.TestCase):
    """Output noise reduction tests."""

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
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {"type": "tool_call", "payload": {"tool": "run_bash", "args": {"command": "echo hi"}}},
        )
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {
                    "tool": "run_bash",
                    "status": "success",
                    "output": "line1\nline2\nline3\n" * 100,
                    "return_code": 0,
                },
            },
        )

        state.apply_event(call_event)
        block = state.apply_event(result_event)

        self.assertIsNotNone(block)
        # The block body should NOT contain the full output
        self.assertNotIn("line1\nline2\nline3", block.body)
        # Should contain the tool name
        self.assertIn("run_bash", block.title)

    def test_tool_result_shows_output_size(self):
        """Tool result summary should include output size."""
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

        self.assertIn("2", block.body)  # 2048 chars ~ 2KB

    def test_failed_tool_shows_error_summary(self):
        """Failed tool should show short error, not full output."""
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

        self.assertIn("command not found", block.body)
        self.assertEqual(block.status, "failed")

    def test_tool_call_shows_args_summary(self):
        """Tool call should show key args, not full args dict."""
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {"tool": "write_file", "args": {"path": "out.py", "content": "x" * 5000}},
            },
        )
        block = state.apply_event(call_event)

        self.assertIn("write_file", block.title)
        self.assertIn("out.py", block.title)
        # Should not contain the full content
        self.assertNotIn("x" * 5000, block.title)

    def test_parallel_tool_call_shows_nested_tool_count(self):
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {
                    "tool": "parallel",
                    "args": {
                        "tool_uses": [
                            {"tool_name": "list_files", "arguments": {"directory": "."}},
                            {"tool_name": "read_file", "arguments": {"path": "README.md"}},
                            {"tool_name": "repo_search", "arguments": {"pattern": "parallel"}},
                        ]
                    },
                },
            },
        )
        block = state.apply_event(call_event)

        self.assertEqual(block.title, "parallel(同时执行了3个工具)")

    def test_parallel_tool_result_shows_nested_success_counts(self):
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {
                    "tool": "parallel",
                    "args": {"tool_uses": [{"tool_name": "list_files"}, {"tool_name": "read_file"}]},
                },
            },
        )
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {
                    "tool": "parallel",
                    "status": "success",
                    "output": "[redacted parallel output: 4096 chars]",
                    "metadata": {
                        "tool_use_count": 2,
                        "success_count": 1,
                        "failed_count": 1,
                    },
                },
            },
        )

        state.apply_event(call_event)
        block = state.apply_event(result_event)

        self.assertIn("同时执行了2个工具", block.body)
        self.assertIn("success=1", block.body)
        self.assertIn("failed=1", block.body)

    def test_profile_route_decision_shows_elapsed_time(self):
        state = self._make_state()

        block = state.apply_event({
            "type": "profile_route_decision",
            "payload": {
                "profile": "coding-agent",
                "action": "direct_answer",
                "turn_mode": "direct_answer",
                "confidence": 0.91,
                "reason": "coding task",
                "fallback_used": False,
                "elapsed_ms": 123.4,
            },
        })

        self.assertEqual(block.title, "profile route")
        self.assertIn("profile=coding-agent", block.body)
        self.assertIn("confidence=0.91", block.body)
        self.assertIn("elapsed=123ms", block.body)
        self.assertNotIn("direct_answer", block.body)
        self.assertNotIn("mode=", block.body)

    def test_tool_timing_from_call_to_result(self):
        """Tool summary should show elapsed time."""
        import time
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {"tool": "run_bash", "args": {"command": "sleep 0"}},
            },
        )
        state.apply_event(call_event)

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

    def test_turn_summary_is_hidden_and_does_not_collapse_details(self):
        state = self._make_state()
        events = [
            {"type": "user_input", "payload": {"turn": 1, "text": "fix it"}},
            {"type": "turn_started", "payload": {"turn": 1}},
            {"type": "tool_call", "payload": {"tool": "run_bash", "args": {"command": "pytest"}}},
            {"type": "tool_result", "payload": {"tool": "run_bash", "status": "success", "output": "ok"}},
            {"type": "file_change", "payload": {"path": "app.py"}},
            {"type": "assistant_message", "payload": {"turn": 1, "text": "done"}},
            {"type": "turn_summary", "payload": {"turn": 1, "summary": "- done", "fold_details": True}},
        ]

        for event in events:
            state.add_block(state.apply_event(event))

        visible = state.visible_blocks()
        self.assertNotIn(1, state.collapsed_turns)
        self.assertFalse(any(block.kind == "summary" for block in visible))
        self.assertTrue(any(block.kind == "user" for block in visible))
        self.assertTrue(any(block.kind == "assistant" for block in visible))
        self.assertTrue(any(block.kind == "tool" for block in visible))
        self.assertTrue(any(block.kind == "file" for block in visible))

    def test_toggle_latest_turn_details_noops_without_folded_summary(self):
        state = self._make_state()
        for event in [
            {"type": "user_input", "payload": {"turn": 1, "text": "fix it"}},
            {"type": "tool_result", "payload": {"tool": "run_bash", "status": "success"}},
            {"type": "turn_summary", "payload": {"turn": 1, "summary": "- done", "fold_details": True}},
        ]:
            state.add_block(state.apply_event(event))

        self.assertTrue(any(block.kind == "tool" for block in state.visible_blocks()))
        self.assertFalse(state.toggle_latest_turn_details())
        self.assertTrue(any(block.kind == "tool" for block in state.visible_blocks()))

    def test_failure_remains_visible_while_approval_and_summary_are_hidden(self):
        state = self._make_state()
        for event in [
            {"type": "user_input", "payload": {"turn": 1, "text": "fix it"}},
            {"type": "failure", "payload": {"category": "runtime_error", "message": "failed"}},
            {"type": "approval_requested", "payload": {"tool": "run_bash", "risk": "shell_risky"}},
            {"type": "agent_fallback", "payload": {"reason": "max_iterations"}},
            {"type": "turn_summary", "payload": {"turn": 1, "summary": "- blocked", "fold_details": True}},
        ]:
            state.add_block(state.apply_event(event))

        visible_kinds = [block.kind for block in state.visible_blocks()]
        self.assertIn("failure", visible_kinds)
        self.assertNotIn("approval", visible_kinds)
        self.assertNotIn("summary", visible_kinds)


class TuiThoughtTests(unittest.TestCase):
    """Thought hiding tests."""

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
        self.assertIn("thought for", block.body)
        self.assertIn("12", block.body)

    def test_thought_block_does_not_contain_reasoning_text(self):
        """Thought block should never contain actual reasoning content."""
        state = self._make_state()

        event = SimpleNamespace(
            to_dict=lambda: {
                "type": "thought_finished",
                "payload": {"duration_seconds": 5.0, "truncated": False, "source": "deepseek"},
            },
        )
        block = state.apply_event(event)
        self.assertNotIn("reasoning", block.body.lower())

    def test_thought_toggle_stores_state(self):
        """Ctrl+T should toggle thought metadata visibility."""
        state = self._make_state()
        self.assertFalse(state.show_thought_details)

        state.toggle_thought_details()
        self.assertTrue(state.show_thought_details)

        state.toggle_thought_details()
        self.assertFalse(state.show_thought_details)


class TuiCancellationTests(unittest.TestCase):
    """Cancellation tests."""

    def test_cancellation_token_cancel_sets_flag(self):
        from harness_code_agent.agent.cancellation import CancellationToken
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


class TuiRichRenderTests(unittest.TestCase):
    """Tests for block_to_rich rendering."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_user_block_renders_with_blue_border(self):
        block = TranscriptBlock("user", "user turn 1", "fix the bug")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("You", rendered.renderables[0].plain)

    def test_assistant_block_renders_with_green_border(self):
        block = TranscriptBlock("assistant", "assistant", "I'll fix it.")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("Assistant", rendered.renderables[0].plain)

    def test_tool_block_renders_with_yellow_border(self):
        block = TranscriptBlock("tool", "read_file(path=x.py)", "", "running")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("read_file", str(rendered))

    def test_thought_block_renders_with_purple_border(self):
        block = TranscriptBlock("thought", "thinking", "thought for 12.3s", "thought")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("thinking", str(rendered))

    def test_failure_block_renders_with_red_border(self):
        block = TranscriptBlock("failure", "failure", "something went wrong", "failed")
        rendered = block_to_rich(block)
        self.assertIn("Error", rendered.title)

    def test_status_bar_renders_from_snapshot(self):
        from rich.text import Text
        bar = StatusBar()
        bar.profile = "coding-agent"
        bar.model = "gpt-4"
        bar.turn = 3
        bar.status = "idle"
        text = bar.render()
        self.assertIsInstance(text, Text)
        plain = text.plain
        self.assertIn("coding-agent", plain)
        self.assertIn("gpt-4", plain)
        self.assertIn("T: 3", plain)

    def test_context_bar_color_thresholds(self):
        from rich.text import Text
        bar = ContextBar()

        # Green below the warning band.
        bar.context_percent = 50
        bar.permission_mode = "workspace-write"
        text = bar.render()
        self.assertIsInstance(text, Text)
        self.assertIn("auto-compact @85%", text.plain)
        self.assertIn("mode: workspace-write", text.plain)
        self.assertTrue(any("50%" in text.plain[span.start:span.end] and "#a3be8c" in str(span.style) for span in text.spans))
        bar.permission_mode = "llm-auto"
        text = bar.render()
        self.assertIn("mode: llm-auto", text.plain)
        self.assertTrue(any("llm-auto" in text.plain[span.start:span.end] and "#ebcb8b" in str(span.style) for span in text.spans))
        self.assertNotIn("Ctrl+P", text.plain)

        # Yellow near the single 85% auto-compact trigger.
        bar.context_percent = 80
        text = bar.render()
        self.assertIn("80%", text.plain)
        self.assertTrue(any("80%" in text.plain[span.start:span.end] and "#ebcb8b" in str(span.style) for span in text.spans))

        # Red at or above 85%.
        bar.context_percent = 85
        text = bar.render()
        self.assertIn("85%", text.plain)
        self.assertTrue(any("85%" in text.plain[span.start:span.end] and "#bf616a" in str(span.style) for span in text.spans))

    def test_context_bar_uses_configured_compaction_threshold_label(self):
        bar = ContextBar()
        snapshot = SessionStatusSnapshot(
            profile="coding-agent",
            model="gpt-4o",
            provider="auto",
            permission_mode="workspace-write",
            session_id="s1",
            cwd=self.root,
            context_tokens=10_000,
            context_window_tokens=100_000,
            context_compact_threshold=80_000,
        )

        bar.update_from_snapshot(snapshot)

        self.assertIn("auto-compact @80%", bar.render().plain)

    def test_plan_panel_renders_completed_steps_with_strike_style(self):
        panel = PlanPanel()
        panel.update_steps(
            [
                PlanStep("write failing test", "completed"),
                PlanStep("implement panel", "current"),
                PlanStep("run tests", "pending"),
            ]
        )

        text = panel.render()
        self.assertIn("write failing test", text.plain)
        self.assertIn("implement panel", text.plain)
        self.assertIn("run tests", text.plain)

        struck_segments = [
            text.plain[span.start:span.end]
            for span in text.spans
            if "strike" in str(span.style)
        ]
        self.assertIn("write failing test", struck_segments)


class TuiAppTests(unittest.TestCase):
    """Tests for the Textual TuiApp."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_app(self, **kwargs):
        """Create a TuiApp with mocked InteractiveSession."""
        from harness_code_agent.tui.app import TuiApp
        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
            mock_session = SimpleNamespace(
                profile=SimpleNamespace(name=lambda: "coding-agent"),
                session=SimpleNamespace(id="test-session"),
                cwd=str(self.root),
                permission_mode="workspace-write",
                conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
                close=lambda: None,
                handle_slash_command=lambda line: True,
                submit=lambda text, cancellation_token=None: SimpleNamespace(notice="", checkpoint=""),
                interrupt_current_shell=lambda: None,
            )
            MockSession.return_value = mock_session
            app = TuiApp(cwd=self.root, profile_name="coding-agent", **kwargs)
        return app

    def test_tui_app_creates_instance(self):
        """TuiApp should instantiate without errors."""
        app = self._make_app()
        self.assertIsNotNone(app)
        self.assertEqual(app.profile_name, "coding-agent")

    def test_tui_app_has_cancellation_token(self):
        """TuiApp should have a cancellation token."""
        from harness_code_agent.agent.cancellation import CancellationToken
        app = self._make_app()
        self.assertIsInstance(app._cancellation_token, CancellationToken)

    def test_tui_app_cancel_sets_token(self):
        """action_cancel should set the cancellation token."""
        app = self._make_app()
        self.assertFalse(app._cancellation_token.is_cancelled)
        app._submitting = True  # Simulate running state
        app.action_cancel()
        self.assertTrue(app._cancellation_token.is_cancelled)

    def test_tui_app_state_initializes_on_mount(self):
        """State should be None before mount, initialized after."""
        app = self._make_app()
        # Before mount, state is None (created in on_mount)
        self.assertIsNone(app.state)

    def test_welcome_shows_general_session_id_after_immediate_bind(self):
        from harness_code_agent.tui.state import SessionStatusSnapshot
        from harness_code_agent.tui.widgets import welcome_rich

        snapshot = SessionStatusSnapshot(
            profile="general",
            model="gpt-4o",
            provider="auto",
            permission_mode="workspace-write",
            session_id="session-123",
            cwd=self.root,
            status="idle",
        )

        plain = welcome_rich(snapshot).renderable.plain

        self.assertIn("session session-123", plain)
        self.assertIn("general", plain)
        self.assertNotIn("None", plain)

    def test_tui_app_run_returns_zero_for_cli_contract(self):
        """run() should preserve the CLI contract of returning process code 0."""
        from harness_code_agent.tui.app import TuiApp

        async def auto_exit(pilot):
            pilot.app.exit()

        with patch("harness_code_agent.tui.app.InteractiveSession") as MockSession:
            mock_session = SimpleNamespace(
                profile=SimpleNamespace(name=lambda: "coding-agent"),
                session=SimpleNamespace(id="test-session"),
                cwd=str(self.root),
                permission_mode="workspace-write",
                conversation=SimpleNamespace(messages=[{"role": "system", "content": "test"}]),
                close=lambda: None,
                handle_slash_command=lambda line: True,
                submit=lambda text, cancellation_token=None: SimpleNamespace(notice="", checkpoint=""),
                interrupt_current_shell=lambda: None,
            )
            MockSession.return_value = mock_session
            app = TuiApp(cwd=self.root, profile_name="coding-agent")

            self.assertEqual(app.run(headless=True, auto_pilot=auto_exit), 0)

    def test_tui_state_recovers_from_blocked_after_turn_started(self):
        from harness_code_agent.tui.state import TuiState, SessionStatusSnapshot

        state = TuiState(
            SessionStatusSnapshot(
                profile="coding-agent",
                model="gpt-4o",
                provider="auto",
                permission_mode="workspace-write",
                session_id="s1",
                cwd=self.root,
                status="running",
            )
        )

        # Simulate agent_fallback → status becomes blocked
        state.apply_event({
            "type": "agent_fallback",
            "payload": {"reason": "loop_detected", "limit_type": "tool_fingerprint"},
        })
        self.assertEqual(state.snapshot.status, "blocked")

        # Simulate next turn_started → status recovers to running
        state.apply_event({
            "type": "turn_started",
            "payload": {"turn": 2},
        })
        self.assertEqual(state.snapshot.status, "running")


if __name__ == "__main__":
    unittest.main()
