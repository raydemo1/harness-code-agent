"""Tests for context compaction strategy (PLAN.md)."""
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _install_fake_openai_module() -> None:
    if "openai" in sys.modules:
        return
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent import config


# ---------------------------------------------------------------------------
# CompactionGate — controls when compaction is allowed
# ---------------------------------------------------------------------------

class CompactionGateTests(unittest.TestCase):
    """CompactionGate tracks active tool calls, dirty state, and message revision."""

    def _make_gate(self):
        from harness_code_agent.agent.compaction import CompactionGate
        return CompactionGate()

    def test_initial_state_allows_compaction(self):
        gate = self._make_gate()
        self.assertTrue(gate.can_compact())

    def test_active_tool_calls_block_compaction(self):
        gate = self._make_gate()
        gate.begin_tool_call()
        gate.begin_tool_call()
        self.assertFalse(gate.can_compact())

    def test_tool_result_allows_compaction(self):
        gate = self._make_gate()
        gate.begin_tool_call()
        gate.begin_tool_call()
        gate.end_tool_call()
        gate.end_tool_call()
        self.assertTrue(gate.can_compact())

    def test_partial_tool_calls_still_block(self):
        gate = self._make_gate()
        gate.begin_tool_call()
        gate.begin_tool_call()
        gate.end_tool_call()
        self.assertFalse(gate.can_compact())

    def test_message_revision_increments(self):
        gate = self._make_gate()
        r0 = gate.revision
        gate.bump_revision()
        r1 = gate.revision
        self.assertEqual(r1, r0 + 1)

    def test_coalescing_window_prevents_rapid_compaction(self):
        gate = self._make_gate()
        gate.mark_compacted()
        # Within coalescing window, should not compact again
        self.assertFalse(gate.can_compact(coalesce_seconds=30))

    def test_coalescing_window_expires(self):
        import time
        gate = self._make_gate()
        gate._last_compact_time = time.time() - 60
        self.assertTrue(gate.can_compact(coalesce_seconds=30))

    def test_dirty_flag_tracks_context_changes(self):
        gate = self._make_gate()
        self.assertFalse(gate.dirty)
        gate.mark_dirty()
        self.assertTrue(gate.dirty)
        gate.mark_compacted()
        self.assertFalse(gate.dirty)


# ---------------------------------------------------------------------------
# Threshold behavior — token usage triggers different actions
# ---------------------------------------------------------------------------

class CompactionThresholdTests(unittest.TestCase):
    """Token usage thresholds drive compaction strategy."""

    def test_thresholds_derived_from_context_window(self):
        """Thresholds should be percentages of HARNESS_CONTEXT_WINDOW_TOKENS."""
        with patch.dict(os.environ, {"HARNESS_CONTEXT_WINDOW_TOKENS": "100000"}):
            from harness_code_agent.agent import compaction
            thresholds = compaction.get_thresholds(100000)
            self.assertEqual(thresholds.compact, 85000)          # 85%

    def test_below_compact_threshold_no_action(self):
        from harness_code_agent.agent.compaction import compaction_action
        from harness_code_agent.agent.compaction import get_thresholds
        thresholds = get_thresholds(128000)
        action = compaction_action(thresholds.compact - 1, thresholds)
        self.assertEqual(action, "none")

    def test_at_compact_threshold_triggers_auto_compact(self):
        from harness_code_agent.agent.compaction import compaction_action
        from harness_code_agent.agent.compaction import get_thresholds
        thresholds = get_thresholds(128000)
        action = compaction_action(thresholds.compact, thresholds)
        self.assertEqual(action, "auto_compact")


# ---------------------------------------------------------------------------
# compact_messages — the core summarization function
# ---------------------------------------------------------------------------

class CompactMessagesTests(unittest.TestCase):
    """Updated compact_messages: user role summary, token budget, tool_call protection."""

    def _mock_llm(self, return_value="summary"):
        return MagicMock(return_value=return_value)

    def test_summary_uses_user_role(self):
        from harness_code_agent.agent.context import compact_messages
        large_old_detail = "important old detail " * 500
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task1 " + large_old_detail},
            {"role": "assistant", "content": "done1 " + large_old_detail},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "done2"},
            {"role": "user", "content": "current task"},
        ]
        result = compact_messages(
            messages,
            self._mock_llm(),
            recent_tail_budget_tokens=100,
        )
        # Find the compacted summary message
        summary_msgs = [m for m in result if m.get("role") == "user" and "COMPACTED" in (m.get("content") or "")]
        self.assertEqual(len(summary_msgs), 1)

    def test_preserves_system_prompt(self):
        from harness_code_agent.agent.context import compact_messages
        large_old_detail = "important old detail " * 500
        messages = [
            {"role": "system", "content": "system instructions"},
            {"role": "user", "content": "task " + large_old_detail},
            {"role": "assistant", "content": "response " + large_old_detail},
            {"role": "user", "content": "current"},
        ]
        result = compact_messages(
            messages,
            self._mock_llm(),
            recent_tail_budget_tokens=100,
        )
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "system instructions")

    def test_protects_tool_call_tool_result_pairs(self):
        from harness_code_agent.agent.context import compact_messages
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "function": {"name": "run_bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "output"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "done2"},
            {"role": "user", "content": "task3"},
            {"role": "assistant", "content": "done3"},
            {"role": "user", "content": "current"},
        ]
        result = compact_messages(messages, self._mock_llm())
        # The tool_call + tool result pair should be kept together in either
        # old (summarized) or recent (kept raw), never split.
        contents = " ".join(str(m) for m in result)
        # If tc1 appears, its tool result must also appear
        if "tc1" in contents:
            self.assertIn("output", contents)

    def test_normal_compaction_skips_current_user_turn(self):
        """Normal compaction (below force threshold) should not compact the current user turn."""
        from harness_code_agent.agent.context import compact_messages
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old task 1"},
            {"role": "assistant", "content": "old response 1"},
            {"role": "user", "content": "old task 2"},
            {"role": "assistant", "content": "old response 2"},
            {"role": "user", "content": "current task"},
        ]
        result = compact_messages(messages, self._mock_llm(), force=False)
        # The last user message "current task" should remain as-is
        last_user = [m for m in result if m.get("role") == "user" and "COMPACTED" not in (m.get("content") or "")]
        self.assertTrue(any("current task" in (m.get("content") or "") for m in last_user))

    def test_respects_target_token_budget_for_recent_tail(self):
        from harness_code_agent.agent.context import compact_messages
        messages = [{"role": "system", "content": "sys"}]
        for i in range(8):
            messages.extend([
                {"role": "user", "content": f"task {i}"},
                {"role": "assistant", "content": "x" * 2000},
            ])
        messages.append({"role": "user", "content": "current task"})

        result = compact_messages(
            messages,
            self._mock_llm("summary"),
            force=True,
            target_tokens=1200,
        )

        from harness_code_agent.agent.context import count_tokens
        self.assertLessEqual(count_tokens(result), 1200)
        self.assertEqual(result[-1]["content"], "current task")

    def test_token_bounded_recent_tail_drops_huge_recent_message(self):
        from harness_code_agent.agent.context import compact_messages

        huge_recent = "HUGE_RECENT_OUTPUT " + ("x " * 8000)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old task " + ("old detail " * 500)},
            {"role": "assistant", "content": "old answer " + ("old detail " * 500)},
            {"role": "user", "content": "recent task"},
            {"role": "assistant", "content": huge_recent},
            {"role": "user", "content": "current task"},
            {"role": "assistant", "content": "current ack"},
        ]

        result = compact_messages(
            messages,
            self._mock_llm("summary"),
            force=True,
            recent_tail_budget_tokens=80,
        )

        rendered = " ".join(str(message.get("content", "")) for message in result)
        self.assertNotIn("HUGE_RECENT_OUTPUT", rendered)
        self.assertEqual(result[-2]["content"], "current task")
        self.assertEqual(result[-1]["content"], "current ack")

    def test_compact_messages_skips_small_region_without_summary_call(self):
        from harness_code_agent.agent.context import compact_messages

        llm = self._mock_llm("should not be called")
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "tiny old task"},
            {"role": "assistant", "content": "tiny old answer"},
            {"role": "user", "content": "tiny old task 2"},
            {"role": "assistant", "content": "tiny old answer 2"},
            {"role": "user", "content": "tiny old task 3"},
            {"role": "assistant", "content": "tiny old answer 3"},
            {"role": "user", "content": "current task"},
        ]

        result = compact_messages(messages, llm, force=False)

        self.assertEqual(result, messages)
        llm.assert_not_called()

    def test_request_token_count_includes_tool_schema_overhead(self):
        from harness_code_agent.agent.context import count_request_tokens, count_tokens

        messages = [{"role": "system", "content": "sys"}]
        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": "large_tool",
                    "description": "schema overhead " * 200,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        self.assertGreater(
            count_request_tokens(messages, tool_schemas=tool_schemas),
            count_tokens(messages),
        )

    def test_summarize_older_conversation_preserves_current_turn(self):
        from harness_code_agent.agent.context import summarize_older_conversation

        large_old_detail = "important old detail " * 500
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old task " + large_old_detail},
            {"role": "assistant", "content": "old result " + large_old_detail},
            {"role": "user", "content": "current task"},
            {"role": "assistant", "content": "current work"},
        ]

        summarized = summarize_older_conversation(
            messages,
            self._mock_llm("older summary"),
            current_turn_start_index=3,
        )

        self.assertEqual(summarized[0], messages[0])
        self.assertIn("older summary", summarized[1]["content"])
        self.assertEqual(summarized[-2:], messages[-2:])

    def test_summarize_older_conversation_skips_small_region_without_summary_call(self):
        from harness_code_agent.agent.context import summarize_older_conversation

        llm = self._mock_llm("should not be called")
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "tiny old task"},
            {"role": "assistant", "content": "tiny old result"},
            {"role": "user", "content": "current task"},
        ]

        summarized = summarize_older_conversation(
            messages,
            llm,
            current_turn_start_index=3,
        )

        self.assertEqual(summarized, messages)
        llm.assert_not_called()

    def test_handoff_reset_persists_handoff_and_restores_fresh_context(self):
        from harness_code_agent.agent.context import create_handoff_reset, restore_from_handoff_reset

        old_output = "OLD_FULL_TOOL_OUTPUT" * 500
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old branch solved"},
            {"role": "tool", "tool_call_id": "old", "content": old_output},
            {"role": "assistant", "content": "old branch resolved"},
            {"role": "user", "content": "current task"},
            {"role": "assistant", "content": "recent error: pytest failed"},
        ]
        state = {
            "current_user_task": "current task",
            "active_plan_status": "step 2 in progress",
            "changed_files": ["app.py"],
            "files_touched": ["app.py", "tests/test_app.py"],
            "recent_errors": ["pytest failed"],
            "failed_commands": ["pytest tests/test_app.py"],
            "active_constraints": ["do not reset profile"],
            "latest_checkpoint_summary": "checkpoint summary",
            "next_recommended_action": "fix failing assertion",
        }

        handoff, path = create_handoff_reset(
            messages,
            state,
            lambda _request: (
                "# Handoff Reset\n\n"
                "## Summary\ncurrent task\n\n"
                "## Suggested Skills\n- diagnose\n\n"
                "## Next Steps\nfix failing assertion"
            ),
            session_id="session/test",
            profile="coding-agent",
            workspace="C:/repo",
            max_turns=5,
        )
        rebuilt = restore_from_handoff_reset(handoff, "sys", path)
        text = "\n".join(str(item.get("content", "")) for item in rebuilt)

        self.assertTrue(path.exists())
        self.assertNotIn(str(Path.cwd()), str(path))
        self.assertEqual(rebuilt[0], messages[0])
        self.assertIn("HANDOFF RESET", text)
        self.assertIn(str(path), text)
        self.assertIn("current task", text)
        self.assertIn("fix failing assertion", text)
        self.assertIn("Suggested Skills", text)
        self.assertNotIn("OLD_FULL_TOOL_OUTPUT", text)


class ContextAnxietyTests(unittest.TestCase):
    def test_detect_anxiety_returns_structured_soft_signal(self):
        from harness_code_agent.agent.context import detect_anxiety

        signal = detect_anxiety([
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "Due to the context limit, let me wrap up here."},
        ])

        self.assertTrue(signal.detected)
        self.assertTrue(signal)
        self.assertGreaterEqual(signal.score, 2)
        self.assertEqual(signal.source, "assistant_recent_messages")
        self.assertTrue(any("context limit" in reason.lower() for reason in signal.reasons))

    def test_detect_anxiety_absent_signal_is_falsey(self):
        from harness_code_agent.agent.context import detect_anxiety

        signal = detect_anxiety([
            {"role": "assistant", "content": "I will continue with the next verification step."},
        ])

        self.assertFalse(signal)
        self.assertFalse(signal.detected)
        self.assertEqual(signal.score, 0)
        self.assertEqual(signal.reasons, [])

    def test_detect_anxiety_recognizes_chinese_context_limit_language(self):
        from harness_code_agent.agent.context import detect_anxiety

        signal = detect_anxiety([
            {"role": "assistant", "content": "我快没上下文空间了，先收尾一下。上下文快满了，我只覆盖关键部分。"},
        ])

        self.assertTrue(signal.detected)
        self.assertGreaterEqual(signal.score, 2)
        self.assertTrue(any("上下文" in reason for reason in signal.reasons))


# ---------------------------------------------------------------------------
# Manual checkpoint restore regression — no implicit git context injection
# ---------------------------------------------------------------------------

class CheckpointRestoreRegressionTests(unittest.TestCase):
    """Manual checkpoint restore must not inject git context."""

    def test_restore_from_checkpoint_no_git_injection(self):
        from harness_code_agent.agent.context import restore_from_checkpoint
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="some git output", returncode=0)
            result = restore_from_checkpoint("checkpoint text", "system prompt")
            # Should NOT contain git context
            user_content = result[1]["content"]
            self.assertNotIn("Recent code changes", user_content)
            self.assertNotIn("git diff", user_content)
            self.assertIn("checkpoint text", user_content)

    def test_restore_preserves_system_prompt(self):
        from harness_code_agent.agent.context import restore_from_checkpoint
        with patch("subprocess.run", side_effect=Exception("no git")):
            result = restore_from_checkpoint("handoff", "my system prompt")
            self.assertEqual(result[0]["content"], "my system prompt")


# ---------------------------------------------------------------------------
# /compact slash command
# ---------------------------------------------------------------------------

class CompactCommandTests(unittest.TestCase):
    """Tests for /compact and its subcommands."""

    def _make_session(self, tmpdir):
        """Create a minimal mock session for command testing."""
        session = MagicMock()
        session.cwd = Path(tmpdir)
        session.conversation = MagicMock()
        session.conversation.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "done"},
        ]
        return session

    def test_compact_command_is_registered_for_summary_view(self):
        """`/compact show` is the only slash-command compaction surface."""
        from harness_code_agent.tui.commands import default_command_registry
        registry = default_command_registry()
        spec = registry._by_name.get("/compact")
        self.assertIsNotNone(spec, "/compact command should be registered")
        self.assertEqual(spec.usage, "/compact show")

    def test_compact_show_subcommand(self):
        """`/compact show` displays the latest compacted summary."""
        from harness_code_agent.tui.commands import default_command_registry
        registry = default_command_registry()
        spec = registry._by_name.get("/compact")
        self.assertIsNotNone(spec)

    def test_compact_show_reads_latest_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from harness_code_agent.tui.commands import default_command_registry

            session = self._make_session(tmpdir)
            session.conversation.messages.insert(1, {
                "role": "user",
                "content": "[COMPACTED CONTEXT — summary of older conversation]\nLatest summary text",
            })
            registry = default_command_registry()

            show = registry.execute("/compact show", session)

            self.assertIn("Latest summary text", show.text)

    def test_compact_show_prefers_persisted_latest_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from harness_code_agent.tui.commands import default_command_registry

            compacted_dir = Path(tmpdir) / "compacted"
            compacted_dir.mkdir()
            (compacted_dir / "latest.md").write_text("Persisted summary text\n", encoding="utf-8")
            session = self._make_session(tmpdir)
            session.session = MagicMock(compacted_dir=compacted_dir)
            session.conversation.messages.insert(1, {
                "role": "user",
                "content": "[COMPACTED CONTEXT]\nMessage summary text",
            })

            show = default_command_registry().execute("/compact show", session)

            self.assertIn("Persisted summary text", show.text)
            self.assertNotIn("Message summary text", show.text)

    def test_compact_rejects_manual_status_and_history_commands(self):
        from harness_code_agent.tui.commands import default_command_registry
        registry = default_command_registry()
        session = MagicMock()

        self.assertIn("Usage: /compact show", registry.execute("/compact", session).text)
        self.assertIn("Usage: /compact show", registry.execute("/compact status", session).text)
        self.assertIn("Usage: /compact show", registry.execute("/compact history", session).text)


class AgentConversationCompactionLifecycleTests(unittest.TestCase):
    def test_message_revision_tracks_user_assistant_tool_and_middleware_appends(self):
        from harness_code_agent.agent.loop import Agent

        agent = Agent("test_agent", "sys", use_tools=False)
        conv = agent.start_conversation()
        initial = conv.compaction_gate.revision

        conv.add_user_turn("task")

        self.assertGreater(conv.compaction_gate.revision, initial)

    def test_context_compacted_hook_replaces_dynamic_context_block(self):
        from harness_code_agent.agent.loop import Agent
        from harness_code_agent.runtime.middleware import AgentMiddleware

        class RefreshMiddleware(AgentMiddleware):
            def on_conversation_start(self, messages, runtime_state=None, agent_name=None):
                return [{"role": "system", "content": "[HARNESS_DYNAMIC_CONTEXT:test]\nold"}]

            def on_context_compacted(self, messages, runtime_state=None, agent_name=None, phase=None):
                return [{"role": "system", "content": f"[HARNESS_DYNAMIC_CONTEXT:test]\nnew:{phase}"}]

        conv = Agent(
            "test_agent",
            "sys",
            use_tools=False,
            middlewares=[RefreshMiddleware()],
        ).start_conversation("task")

        conv._refresh_dynamic_context_after_compaction(phase="summarizing_history")

        dynamic = [
            message for message in conv.messages
            if str(message.get("content") or "").startswith("[HARNESS_DYNAMIC_CONTEXT:test]")
        ]
        self.assertEqual(len(dynamic), 1)
        self.assertIn("new:summarizing_history", dynamic[0]["content"])
        self.assertEqual(conv.messages[1], dynamic[0])

    def test_auto_compaction_summarizes_and_returns_below_threshold_after_summary(self):
        from harness_code_agent.agent.compaction import get_thresholds
        from harness_code_agent.agent.loop import Agent

        thresholds = get_thresholds()
        agent = Agent("test_agent", "sys", use_tools=False)
        conv = agent.start_conversation("task")
        summarized_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[COMPACTED CONTEXT]\nsummary"},
            {"role": "user", "content": "task"},
        ]

        with (
            patch(
                "harness_code_agent.agent.conversation.context.count_tokens",
                side_effect=[
                    thresholds.compact,
                    thresholds.compact - 1,
                ],
            ),
            patch("harness_code_agent.agent.conversation.context.detect_anxiety", return_value=False),
            patch("harness_code_agent.agent.conversation.context.summarize_older_conversation", return_value=summarized_messages) as summarize,
            patch.object(
                conv,
                "_request_assistant_message",
                return_value=({"role": "assistant", "content": "done"}, "stop"),
            ),
        ):
            conv.run_until_idle()

        summarize.assert_called_once()

    def test_auto_compaction_persists_latest_summary(self):
        from harness_code_agent.agent.compaction import get_thresholds
        from harness_code_agent.agent.loop import Agent
        from harness_code_agent.runtime.permissions import PermissionPolicy
        from harness_code_agent.runtime.tool_context import ToolContext
        from harness_code_agent.sessions.store import SessionStore
        from harness_code_agent.workspace.service import WorkspaceService

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = SessionStore(root / ".harness")
            session = store.create(
                profile="coding-agent",
                cwd=root,
                model="test-model",
                permission_mode="workspace-write",
            )
            tool_context = ToolContext(
                workspace=WorkspaceService(root=root, snapshots_dir=session.snapshots_dir),
                permission_policy=PermissionPolicy(mode="workspace-write"),
                event_bus=store.event_bus(session),
                session_id=session.id,
            )
            thresholds = get_thresholds()
            agent = Agent("test_agent", "sys", use_tools=False, tool_context=tool_context)
            conv = agent.start_conversation("task")
            summarized_messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "[COMPACTED CONTEXT]\nPersist me"},
                {"role": "user", "content": "task"},
            ]

            with (
                patch(
                    "harness_code_agent.agent.conversation.context.count_tokens",
                    side_effect=[thresholds.compact] + [thresholds.compact - 1] * 10,
                ),
                patch("harness_code_agent.agent.conversation.context.detect_anxiety", return_value=False),
                patch("harness_code_agent.agent.conversation.context.summarize_older_conversation", return_value=summarized_messages),
                patch.object(
                    conv,
                    "_request_assistant_message",
                    return_value=({"role": "assistant", "content": "done"}, "stop"),
                ),
            ):
                conv.run_until_idle()

            latest = session.compacted_dir / "latest.md"
            self.assertTrue(latest.exists())
            self.assertIn("Persist me", latest.read_text(encoding="utf-8"))

    def test_auto_compaction_suspends_for_turn_when_summary_still_over_threshold(self):
        from harness_code_agent.agent.compaction import get_thresholds
        from harness_code_agent.agent.loop import Agent

        thresholds = get_thresholds()
        agent = Agent("test_agent", "sys", use_tools=False)
        conv = agent.start_conversation("task")

        with (
            patch(
                "harness_code_agent.agent.conversation.context.count_tokens",
                side_effect=[
                    thresholds.compact,
                    thresholds.compact,
                    thresholds.compact,
                ],
            ),
            patch("harness_code_agent.agent.conversation.context.detect_anxiety", return_value=False),
            patch("harness_code_agent.agent.conversation.context.summarize_older_conversation", return_value=conv.messages) as summarize,
            patch("harness_code_agent.agent.conversation.context.create_handoff_reset") as handoff_reset,
            patch.object(
                conv,
                "_request_assistant_message",
                side_effect=[
                    ({"role": "assistant", "content": "continue"}, None),
                    ({"role": "assistant", "content": "done"}, "stop"),
                ],
            ),
        ):
            conv.run_until_idle()

        self.assertEqual(summarize.call_count, 1)
        handoff_reset.assert_not_called()
        self.assertTrue(conv.runtime_state.auto_compaction_suspended)

    def test_handoff_reset_after_two_rapid_refills(self):
        from harness_code_agent.agent.compaction import get_thresholds
        from harness_code_agent.agent.loop import Agent

        thresholds = get_thresholds()
        agent = Agent("test_agent", "sys", use_tools=False)
        conv = agent.start_conversation("first")
        conv.runtime_state.context_refill_streak = 1
        conv.runtime_state.fallback.request_stop(reason="loop_detected")
        conv.runtime_state.recovery.mode = "SPEC_RECHECK"

        with (
            patch(
                "harness_code_agent.agent.conversation.context.count_tokens",
                side_effect=[
                    thresholds.compact,       # main loop → trigger
                    thresholds.compact,       # after summarize → still over → handoff reset
                ],
            ),
            patch("harness_code_agent.agent.conversation.context.detect_anxiety", return_value=False),
            patch("harness_code_agent.agent.conversation.context.summarize_older_conversation", return_value=conv.messages),
            patch(
                "harness_code_agent.agent.conversation.context.create_handoff_reset",
                return_value=("# Handoff\n\n## Suggested Skills\n- diagnose", "C:/tmp/handoff.md"),
            ) as handoff_reset,
            patch.object(
                conv,
                "_request_assistant_message",
                return_value=({"role": "assistant", "content": "done"}, "stop"),
            ),
        ):
            conv.run_until_idle()

        handoff_reset.assert_called_once()
        self.assertIn("[HANDOFF RESET]", conv.messages[1]["content"])
        self.assertIn("C:/tmp/handoff.md", conv.messages[1]["content"])
        self.assertEqual(conv.runtime_state.context_refill_streak, 0)
        self.assertFalse(conv.runtime_state.fallback.stop_requested)
        self.assertEqual(conv.runtime_state.recovery.mode, "NORMAL")

    def test_context_anxiety_below_threshold_only_records_soft_signal(self):
        from harness_code_agent.agent.compaction import get_thresholds
        from harness_code_agent.agent.context import ContextAnxietySignal
        from harness_code_agent.agent.loop import Agent

        thresholds = get_thresholds()
        agent = Agent("test_agent", "sys", use_tools=False)
        conv = agent.start_conversation("task")
        observed: list[dict] = []

        class Bus:
            def emit_event(self, event):
                observed.append(event.to_event().to_dict())

        conv._event_bus = Bus()
        signal = ContextAnxietySignal(
            detected=True,
            score=2,
            reasons=["due to context limit", "let me wrap up"],
        )

        with (
            patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=thresholds.compact - 1),
            patch("harness_code_agent.agent.conversation.context.detect_anxiety", return_value=signal),
            patch("harness_code_agent.agent.conversation.context.summarize_older_conversation") as summarize,
            patch("harness_code_agent.agent.conversation.context.create_handoff_reset") as handoff_reset,
            patch.object(
                conv,
                "_request_assistant_message",
                return_value=({"role": "assistant", "content": "done"}, "stop"),
            ),
        ):
            conv.run_until_idle()

        summarize.assert_not_called()
        handoff_reset.assert_not_called()
        anxiety_events = [event for event in observed if event["type"] == "context_anxiety_observed"]
        self.assertEqual(len(anxiety_events), 1)
        self.assertEqual(anxiety_events[0]["payload"]["score"], 2)
        self.assertEqual(anxiety_events[0]["payload"]["reasons"], ["due to context limit", "let me wrap up"])

    def test_context_anxiety_soft_signal_emits_once_per_turn(self):
        from harness_code_agent.agent.compaction import get_thresholds
        from harness_code_agent.agent.context import ContextAnxietySignal
        from harness_code_agent.agent.loop import Agent

        thresholds = get_thresholds()
        agent = Agent("test_agent", "sys", use_tools=False)
        conv = agent.start_conversation("task")
        observed: list[dict] = []

        class Bus:
            def emit_event(self, event):
                observed.append(event.to_event().to_dict())

        conv._event_bus = Bus()
        signal = ContextAnxietySignal(
            detected=True,
            score=2,
            reasons=["due to context limit", "let me wrap up"],
        )

        with (
            patch("harness_code_agent.agent.conversation.context.count_tokens", return_value=thresholds.compact - 1),
            patch("harness_code_agent.agent.conversation.context.detect_anxiety", return_value=signal),
            patch.object(
                conv,
                "_request_assistant_message",
                side_effect=[
                    ({"role": "assistant", "content": "continue"}, None),
                    ({"role": "assistant", "content": "done"}, "stop"),
                ],
            ),
        ):
            conv.run_until_idle()

        anxiety_events = [event for event in observed if event["type"] == "context_anxiety_observed"]
        self.assertEqual(len(anxiety_events), 1)


# ---------------------------------------------------------------------------
# Compacted storage — persistence in .harness/sessions/<id>/compacted/
# ---------------------------------------------------------------------------

class CompactedStorageTests(unittest.TestCase):
    """Compacted directory structure and persistence."""

    def test_session_dataclass_has_compacted_path(self):
        """Session dataclass should include compacted_dir."""
        from harness_code_agent.sessions.store import Session
        s = Session(
            id="test",
            root=Path("/tmp/test"),
            metadata_path=Path("/tmp/test/session.json"),
            events_path=Path("/tmp/test/events.jsonl"),
            snapshots_dir=Path("/tmp/test/snapshots"),
            summary_path=Path("/tmp/test/summary.md"),
        )
        # compacted_dir should exist as an attribute
        self.assertTrue(hasattr(s, "compacted_dir"))

    def test_session_store_creates_compacted_dir(self):
        """SessionStore.create() should create the compacted/ subdirectory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from harness_code_agent.sessions.store import SessionStore
            store = SessionStore(Path(tmpdir))
            session = store.create(
                profile="coding-agent",
                cwd=tmpdir,
                model="test",
                permission_mode="workspace-write",
            )
            compacted = session.root / "compacted"
            self.assertTrue(compacted.exists())
            self.assertTrue((compacted / "history").exists())


# ---------------------------------------------------------------------------
# Session events for compaction
# ---------------------------------------------------------------------------

class CompactionEventTests(unittest.TestCase):
    """New session event types for compaction lifecycle."""

    def test_compaction_started_event(self):
        from harness_code_agent.sessions.events import ContextCompactionStartedEvent
        event = ContextCompactionStartedEvent(
            token_count=100000,
            threshold=108800,
            forced=False,
        )
        structured = event.to_event()
        self.assertEqual(structured.type, "context_compaction_started")
        self.assertEqual(structured.payload["token_count"], 100000)

    def test_compaction_committed_event(self):
        from harness_code_agent.sessions.events import ContextCompactionCommittedEvent
        event = ContextCompactionCommittedEvent(
            summary_chars=500,
            messages_before=20,
            messages_after=5,
            tokens_saved=40000,
        )
        structured = event.to_event()
        self.assertEqual(structured.type, "context_compaction_committed")
        self.assertIn("tokens_saved", structured.payload)

    def test_context_anxiety_observed_event(self):
        from harness_code_agent.sessions.events import ContextAnxietyObservedEvent
        event = ContextAnxietyObservedEvent(
            token_count=120000,
            threshold=170000,
            score=2,
            reasons=["due to context limit", "let me wrap up"],
        )
        structured = event.to_event()
        self.assertEqual(structured.type, "context_anxiety_observed")
        self.assertEqual(structured.payload["score"], 2)
        self.assertEqual(structured.payload["threshold"], 170000)
        self.assertEqual(structured.payload["reasons"], ["due to context limit", "let me wrap up"])

# ---------------------------------------------------------------------------
# TUI state — compaction events update status bar
# ---------------------------------------------------------------------------

class TuiStateCompactionTests(unittest.TestCase):
    """TUI state.py should handle compaction events."""

    def _make_state(self):
        from harness_code_agent.tui.state import TuiState, SessionStatusSnapshot
        snapshot = SessionStatusSnapshot(
            profile="coding-agent",
            model="test",
            provider="test",
            permission_mode="workspace-write",
            session_id="test-123",
            cwd=Path("/tmp"),
        )
        return TuiState(snapshot=snapshot)

    def test_compaction_started_updates_status(self):
        state = self._make_state()
        event = MagicMock()
        event.to_dict.return_value = {
            "type": "context_compaction_started",
            "payload": {"token_count": 100000, "forced": False},
        }
        block = state.apply_event(event)
        self.assertIsNotNone(block)
        self.assertIn("compact", state.snapshot.status.lower())

    def test_compaction_committed_restores_idle(self):
        state = self._make_state()
        event = MagicMock()
        event.to_dict.return_value = {
            "type": "context_compaction_committed",
            "payload": {"tokens_saved": 40000},
        }
        block = state.apply_event(event)
        self.assertIsNotNone(block)

    def test_handoff_reset_shows_notice(self):
        state = self._make_state()
        event = MagicMock()
        event.to_dict.return_value = {
            "type": "context_compaction_started",
            "payload": {"token_count": 180000, "phase": "handoff_reset"},
        }
        block = state.apply_event(event)
        self.assertIsNotNone(block)
        self.assertIn("handoff reset", block.title.lower())
        self.assertIn("handoff reset", state.snapshot.status.lower())

    def test_context_anxiety_observed_shows_soft_notice(self):
        state = self._make_state()
        event = MagicMock()
        event.to_dict.return_value = {
            "type": "context_anxiety_observed",
            "payload": {"token_count": 120000, "threshold": 170000, "score": 2, "reasons": ["due to context limit"]},
        }
        block = state.apply_event(event)
        self.assertIsNotNone(block)
        self.assertEqual(state.snapshot.status, "context anxiety observed")
        self.assertEqual(block.status, "observed")
        self.assertIn("due to context limit", block.body)


# ---------------------------------------------------------------------------
# Config — context window tokens
# ---------------------------------------------------------------------------

class ContextWindowConfigTests(unittest.TestCase):
    """HARNESS_CONTEXT_WINDOW_TOKENS config."""

    def test_context_window_defaults_to_200k(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_CONTEXT_WINDOW_TOKENS", None)
            os.environ.pop("COMPRESS_THRESHOLD", None)
            import importlib
            from harness_code_agent import config
            importlib.reload(config)
            self.assertEqual(config.CONTEXT_WINDOW_TOKENS, 200000)
            self.assertEqual(config.COMPRESS_THRESHOLD, 170000)
            # Restore default for later tests in this process.
            importlib.reload(config)

    def test_context_window_configurable(self):
        with patch.dict(os.environ, {"HARNESS_CONTEXT_WINDOW_TOKENS": "64000"}):
            os.environ.pop("COMPRESS_THRESHOLD", None)
            # Need to reload to pick up env change
            import importlib
            from harness_code_agent import config
            importlib.reload(config)
            self.assertEqual(config.CONTEXT_WINDOW_TOKENS, 64000)
            self.assertEqual(config.COMPRESS_THRESHOLD, 54400)
            # Restore default
            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
