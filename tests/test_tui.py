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
        bus.emit("plan_ready", agent="main_agent", payload={"profile": "plan"})
        bus.emit("profile_switched", agent="main_agent", payload={"previous_profile": "plan", "profile": "coding-agent", "reason": "execute"})
        bus.emit("turn_finished", agent="main_agent", payload={"turn": 1, "checkpoint": "checkpoint created: abc"})

        for event in events:
            state.add_block(state.apply_event(event))

        self.assertEqual(state.snapshot.profile, "coding-agent")
        self.assertFalse(state.snapshot.pending_plan)
        self.assertEqual(state.snapshot.checkpoint, "checkpoint created: abc")
        self.assertEqual(state.snapshot.running_tool, "")
        self.assertGreaterEqual(len(state.blocks), 5)

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


if __name__ == "__main__":
    unittest.main()
