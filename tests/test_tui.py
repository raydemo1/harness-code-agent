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
from harness_code_agent.tui.commands import default_command_registry
from harness_code_agent.tui.completion import (
    current_mention_query,
    mention_candidates,
    replace_mention_fragment,
)
from harness_code_agent.tui.screens import ApprovalPanel
from harness_code_agent.tui.state import (
    PlanStep,
    SessionStatusSnapshot,
    TranscriptBlock,
    TuiState,
)
from harness_code_agent.tui.widgets import StatusBar, block_to_rich


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_command_registry_is_flat_and_exposes_workflow_entries(self):
        registry = default_command_registry()
        help_text = registry.format_help()
        result = registry.execute("/profile", SimpleNamespace())

        self.assertNotIn("配置:", help_text)
        self.assertNotIn("/swe", help_text)
        self.assertIn("/profile", help_text)
        self.assertIn("/checkpoint", help_text)
        self.assertEqual(result.action, "profile")
        self.assertNotIn("/code", help_text)
        self.assertNotIn("/review", help_text)

    def test_command_registry_reports_validation_errors(self):
        registry = default_command_registry()
        result = registry.execute("/profile nope", SimpleNamespace())

        self.assertIn("用法：/profile", result.text)
        self.assertTrue(result.should_continue)

    def test_user_skills_are_dynamic_agent_commands(self):
        skill_registry = SimpleNamespace(
            user_commands=[
                {
                    "name": "triage",
                    "description": "Triage an issue.",
                    "argument_hint": "<issue>",
                    "path": "skills/triage/SKILL.md",
                }
            ]
        )
        registry = default_command_registry(skill_registry=skill_registry)
        calls = []
        session = SimpleNamespace(
            submit_skill_command=lambda line: calls.append(line) or SimpleNamespace(text="triaged")
        )

        result = registry.execute("/triage 42", session)

        self.assertTrue(registry.is_agent_command("/triage"))
        self.assertEqual(result.text, "triaged")
        self.assertEqual(calls, ["/triage 42"])
        self.assertIn("/triage <issue>", registry.format_help())

    def test_removed_builtin_name_can_be_used_by_a_user_skill(self):
        skill_registry = SimpleNamespace(
            user_commands=[
                {
                    "name": "help",
                    "description": "Conflicts with a built-in.",
                    "argument_hint": "",
                    "path": "skills/help/SKILL.md",
                }
            ]
        )

        registry = default_command_registry(skill_registry=skill_registry)
        self.assertTrue(registry.is_agent_command("/help"))

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

        report = registry.execute("/observe", session)

        self.assertEqual(report.action, "observe")

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

        report = registry.execute("/observe", session)

        self.assertEqual(report.action, "observe")

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
        self.assertIn("当前还没有会话", screen._current_session_body())

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
        skill_candidates = mention_candidates(self.root, "skill:dia", store)

        self.assertIn('file:"space name.md"', [item.insert_text for item in file_candidates])
        self.assertIn("file:README.md", [item.insert_text for item in readme_candidates])
        self.assertNotIn("node_modules/ignored.js", [item.insert_text for item in file_candidates])
        self.assertIn("session:" + session.id, [item.insert_text for item in session_candidates])
        self.assertEqual(skill_candidates, [])

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
            replace_mention_fragment("use @ses", "session:abc"),
            "use @session:abc ",
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

        # Budget warnings stay out of the transcript entirely.
        self.assertIsNone(warning)
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
        self.assertEqual(state.snapshot.status, "idle")

    def test_python_command_does_not_persist_bare_prefix(self):
        self.assertIsNone(_derive_persistent_prefix("python"))
        self.assertEqual(_derive_persistent_prefix("python script.py"), ["python", "script.py"])
        self.assertEqual(
            _derive_persistent_prefix('python focusflow.py "Write the report" 25'),
            ["python", "focusflow.py"],
        )
        self.assertIsNone(_derive_persistent_prefix("python -c \"print('hi')\""))
        self.assertEqual(_derive_persistent_prefix("python -m unittest tests.test_tui"), ["python", "-m", "unittest"])
        self.assertEqual(
            _derive_persistent_prefix('python focusflow.py Task 25; "---"; python focusflow.py Other 15'),
            ["python", "focusflow.py"],
        )

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

    def test_compound_command_does_not_offer_fake_persistence(self):
        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "python -m unittest; python app.py"},
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )
        panel = ApprovalPanel(request)

        self.assertIn("无法信任", panel._format_choices().plain)
        self.assertTrue(panel.handle_key("2"))
        self.assertEqual(panel._selected_index, 0)

    def test_compound_with_one_shared_python_prefix_can_be_trusted(self):
        command = 'python focusflow.py Task 25; "---"; python focusflow.py Other 15'
        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": command},
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )
        panel = ApprovalPanel(request)
        allowlist = ApprovalAllowlist(self.root)
        allowlist.add_prefix_rule(["python", "focusflow.py"], command=command)

        self.assertIn("信任此前缀", panel._format_choices().plain)
        self.assertTrue(allowlist.matches(command))

        smoke_command = (
            'python focusflow.py Task 25 && echo "---" && '
            'python focusflow.py Other 15; echo "exit=$?"'
        )
        self.assertEqual(
            _derive_persistent_prefix(smoke_command),
            ["python", "focusflow.py"],
        )
        self.assertTrue(allowlist.matches(smoke_command))

    def test_approval_waits_for_an_explicit_decision_without_auto_approval(self):
        import threading
        import time

        captured = {}

        class FakeApp:
            def call_from_thread(self, callback):
                callback()

            def show_approval_panel(self, request, event, result_holder):
                captured.update(event=event, result_holder=result_holder)

        provider = TuiApprovalProvider(project_root=self.root, app_tui=FakeApp())
        request = ApprovalRequest(
            tool_name="run_bash",
            args={"command": "python app.py"},
            risk="shell_risky",
            reason="workspace-write mode requires user approval",
        )
        results = []
        worker = threading.Thread(target=lambda: results.append(provider.request(request)))
        worker.start()

        time.sleep(0.05)
        self.assertTrue(worker.is_alive())
        self.assertEqual(results, [])

        captured["result_holder"][0] = True
        captured["event"].set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].approved)


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

    def test_middleware_activity_is_compact_and_names_guidance_source(self):
        state = self._make_state()
        event = SimpleNamespace(
            to_dict=lambda: {
                "type": "middleware_activity",
                "payload": {
                    "tool": "write_file",
                    "hooks": 18,
                    "duration_ms": 1.4,
                    "outcome": "guided",
                    "sources": ["RecoveryStrategyMiddleware"],
                },
            }
        )

        block = state.apply_event(event)

        self.assertEqual(block.kind, "middleware")
        self.assertEqual(block.status, "guided")
        self.assertIn("18 个钩子", block.body)
        self.assertIn("恢复", block.body)

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

    def test_parallel_agents_tool_call_shows_nested_agent_count(self):
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {
                    "tool": "parallel_agents",
                    "args": {
                        "agents": [
                            {"agent_profile": "explore", "task": "inspect"},
                            {"agent_profile": "review", "task": "review"},
                            {"agent_profile": "verify", "task": "verify"},
                        ]
                    },
                },
            },
        )
        block = state.apply_event(call_event)

        self.assertEqual(block.title, "parallel_agents(3 个工具)")

    def test_parallel_tool_result_shows_nested_success_counts(self):
        state = self._make_state()

        call_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_call",
                "payload": {
                    "tool": "parallel_commands",
                    "args": {"commands": [{"command": "git status --short"}, {"command": "python --version"}]},
                },
            },
        )
        result_event = SimpleNamespace(
            to_dict=lambda: {
                "type": "tool_result",
                "payload": {
                    "tool": "parallel_commands",
                    "status": "success",
                    "output": "[redacted parallel_commands output: 4096 chars]",
                    "metadata": {
                        "item_count": 2,
                        "success_count": 1,
                        "failed_count": 1,
                    },
                },
            },
        )

        state.apply_event(call_event)
        block = state.apply_event(result_event)

        self.assertIn("2 个工具", block.body)
        self.assertIn("1 成功", block.body)
        self.assertIn("1 失败", block.body)

    def test_profile_route_decision_shows_clean_summary(self):
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

        self.assertEqual(block.title, "配置路由")
        self.assertIn("coding-agent", block.body)
        # Internal routing metrics stay out of the transcript.
        self.assertNotIn("confidence", block.body)
        self.assertNotIn("elapsed", block.body)
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

        stored_result = next(event for event in events if event.type == "tool_result")
        self.assertEqual(stored_result.payload["output"], large_output)
        self.assertNotIn(large_output, state.blocks[-1].body)

    def test_turn_summary_and_internal_events_stay_out_of_transcript(self):
        state = self._make_state()
        events = [
            {"type": "user_input", "payload": {"turn": 1, "text": "fix it"}},
            {"type": "turn_started", "payload": {"turn": 1}},
            {"type": "tool_call", "payload": {"tool": "run_bash", "args": {"command": "pytest"}}},
            {"type": "tool_result", "payload": {"tool": "run_bash", "status": "success", "output": "ok"}},
            {"type": "file_change", "payload": {"path": "app.py", "operation": "write"}},
            {"type": "assistant_message", "payload": {"turn": 1, "text": "done"}},
            {"type": "turn_summary", "payload": {"turn": 1, "summary": "- done", "fold_details": True}},
        ]

        for event in events:
            state.add_block(state.apply_event(event))

        kinds = [block.kind for block in state.blocks]
        self.assertTrue(any(kind == "user" for kind in kinds))
        self.assertTrue(any(kind == "assistant" for kind in kinds))
        self.assertTrue(any(kind == "tool" for kind in kinds))
        self.assertTrue(any(kind == "file" for kind in kinds))
        self.assertNotIn("summary", kinds)
        self.assertNotIn("session", kinds)

    def test_failure_remains_visible_while_approval_stays_hidden(self):
        state = self._make_state()
        for event in [
            {"type": "user_input", "payload": {"turn": 1, "text": "fix it"}},
            {"type": "failure", "payload": {"category": "runtime_error", "message": "failed"}},
            {"type": "approval_requested", "payload": {"tool": "run_bash", "risk": "shell_risky"}},
            {"type": "agent_fallback", "payload": {"reason": "max_iterations"}},
        ]:
            state.add_block(state.apply_event(event))

        kinds = [block.kind for block in state.blocks]
        self.assertIn("failure", kinds)
        self.assertNotIn("approval", kinds)

    def test_file_change_carries_diff_for_transcript(self):
        state = self._make_state()

        block = state.apply_event({
            "type": "file_change",
            "payload": {
                "path": "src/app.py",
                "operation": "apply_patch",
                "diff": "@@ -1,2 +1,2 @@\n context\n-old line\n+new line",
            },
        })

        self.assertEqual(block.title, "已编辑 src/app.py")
        self.assertIn("-old line", block.body)
        self.assertIn("+new line", block.body)

    def test_file_change_without_diff_stays_single_line(self):
        state = self._make_state()

        block = state.apply_event({
            "type": "file_change",
            "payload": {"path": "src/new.py", "operation": "write_file"},
        })

        self.assertEqual(block.title, "已写入 src/new.py")
        self.assertEqual(block.body, "")

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
        self.assertIn("12.3s", block.body)
        # Internal source identifiers stay out of the transcript.
        self.assertNotIn("source", block.body)

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
        self.assertIn("你", rendered.renderables[0].plain)

    def test_assistant_block_renders_with_green_border(self):
        block = TranscriptBlock("assistant", "assistant", "I'll fix it.")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("助手", rendered.renderables[0].plain)

    def test_large_assistant_block_uses_bounded_plain_rendering(self):
        block = TranscriptBlock("assistant", "assistant", "line\n" * 5000)

        rendered = block_to_rich(block)

        body = rendered.renderables[1]
        self.assertEqual(body.__class__.__name__, "Text")
        self.assertIn("响应过长", body.plain)

    def test_tool_block_renders_with_yellow_border(self):
        block = TranscriptBlock("tool", "read_file(path=x.py)", "", "running")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("读取", str(rendered))
        self.assertNotIn("read_file", str(rendered))

    def test_middleware_block_renders_as_compact_policy_line(self):
        block = TranscriptBlock(
            "middleware",
            "策略管线",
            "write_file  ·  18 个钩子  ·  1.4 毫秒  ·  恢复",
            "guided",
        )

        rendered = block_to_rich(block)

        self.assertEqual(rendered.__class__.__name__, "Text")
        self.assertIn("策略管线", rendered.plain)
        self.assertIn("已引导", rendered.plain)
        self.assertIn("恢复", rendered.plain)

    def test_primary_transcript_content_is_not_dimmed(self):
        user = block_to_rich(TranscriptBlock("user", "user turn 1", "fix the bug"))
        tool = block_to_rich(
            TranscriptBlock("tool", "run_bash(command=pytest)", "48 passed", "success")
        )

        self.assertNotIn("dim", str(user.renderables[1].style))
        result_start = tool.plain.index("48 passed")
        result_styles = [
            span.style
            for span in tool.spans
            if span.end > result_start
        ]
        self.assertTrue(result_styles)
        self.assertTrue(all("dim" not in str(style) for style in result_styles))

    def test_thought_block_renders_dim(self):
        block = TranscriptBlock("thought", "thinking", "for 12.3s", "thought")
        rendered = block_to_rich(block)
        self.assertNotEqual(rendered.__class__.__name__, "Panel")
        self.assertIn("思考", str(rendered))

    def test_failure_block_renders_with_red_border(self):
        block = TranscriptBlock("failure", "failure", "something went wrong", "failed")
        rendered = block_to_rich(block)
        self.assertIn("错误", rendered.title)

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
        self.assertIn("剩余上下文", plain)
        self.assertNotIn("workspace-write", plain)
        self.assertNotIn("turn 3", plain)

    def test_status_bar_shows_live_elapsed_time_for_slow_operations(self):
        bar = StatusBar()
        bar.profile = "coding-agent"
        bar.status = "thinking"
        bar.elapsed_seconds = 73

        self.assertIn("模型思考中  1m 13s", bar.render().plain)

    def test_plan_update_block_renders_full_colored_progress(self):
        rendered = block_to_rich(
            TranscriptBlock(
                "plan",
                "Plan",
                "✓ inspect the bug\n› implement the fix\n○ run tests",
                "updated",
            )
        )

        lines = rendered.renderables[1]
        self.assertIn("inspect the bug", lines.plain)
        self.assertIn("implement the fix", lines.plain)
        self.assertIn("run tests", lines.plain)
        struck = [
            lines.plain[span.start:span.end]
            for span in lines.spans
            if "strike" in str(span.style)
        ]
        self.assertIn("inspect the bug", struck)


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

    def test_tui_app_cancel_sets_token(self):
        """action_cancel should set the cancellation token."""
        app = self._make_app()
        self.assertFalse(app._cancellation_token.is_cancelled)
        app._submitting = True  # Simulate running state
        app.action_cancel()
        self.assertTrue(app._cancellation_token.is_cancelled)

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

        plain = "\n".join(renderable.plain for renderable in welcome_rich(snapshot).renderables)

        self.assertIn("gpt-4o", plain)
        self.assertIn("workspace-write", plain)
        self.assertIn(self.root.name, plain)
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
        from harness_code_agent.tui.state import SessionStatusSnapshot, TuiState

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
