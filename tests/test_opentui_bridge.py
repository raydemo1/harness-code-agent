"""Protocol-level tests for the Bun/Python OpenTUI seam."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness_code_agent.agent.cancellation import CancellationToken
from harness_code_agent.runtime.approvals import ApprovalRequest
from harness_code_agent.runtime.questions import QuestionOption, QuestionRequest
from harness_code_agent.sessions.events import SessionEvent
from harness_code_agent.tui_bridge import BridgeInteractionProvider, BridgeServer


class _FakeSession:
    def __init__(self, **kwargs):
        self._listener = kwargs["event_listener"]
        self._stream = kwargs["stream_sink"]
        self.skill_registry = SimpleNamespace(user_commands=[])
        self.display_routing_mode = "auto"
        self.display_profile = "general"
        self.session_store = SimpleNamespace(
            list_sessions=lambda: [{"id": "old-session", "profile": "general", "created_at": "2026-08-24T00:00:00Z"}],
            read_events=lambda _session_id: [{"type": "user_input", "payload": {"text": "inspect parser", "turn": 1}}],
        )
        self.closed = False
        kwargs["startup_sink"]("loading skills")
        self._listener(
            SessionEvent(
                sequence=1,
                timestamp=0,
                type="session_started",
                agent="main_agent",
                payload={
                    "session_id": "fake-session",
                    "profile": "general",
                    "workspace": str(Path.cwd()),
                },
            )
        )

    def submit(self, text, cancellation_token=None):
        self._listener(SessionEvent(2, 0, "turn_started", "main_agent", {"turn": 1}))
        self._listener(SessionEvent(3, 0, "user_input", "main_agent", {"text": text, "turn": 1}))
        self._stream("hello")
        self._listener(SessionEvent(4, 0, "assistant_message", "main_agent", {"text": "hello", "streamed": True}))
        self._listener(SessionEvent(5, 0, "turn_finished", "main_agent", {"turn": 1}))
        return SimpleNamespace(notice="", checkpoint="")

    def interrupt_current_shell(self):
        return False

    def close(self):
        self.closed = True


class _SlowFirstDeltaSession(_FakeSession):
    started = threading.Event()
    release = threading.Event()

    def submit(self, text, cancellation_token=None):
        type(self).started.set()
        type(self).release.wait(timeout=2)
        if cancellation_token is not None:
            cancellation_token.check()
        return super().submit(text, cancellation_token=cancellation_token)


class OpenTuiBridgeTests(unittest.TestCase):
    def setUp(self):
        _SlowFirstDeltaSession.started.clear()
        _SlowFirstDeltaSession.release.clear()

    def test_interaction_provider_waits_for_approval_resolution(self):
        events = []
        provider = BridgeInteractionProvider(events.append, project_root=Path.cwd())
        request = ApprovalRequest(
            tool_name="write_file",
            args={"path": "README.md"},
            risk="edit",
            reason="workspace write requires approval",
        )
        result_holder = []

        worker = threading.Thread(target=lambda: result_holder.append(provider.request(request)))
        worker.start()
        self.assertTrue(provider.wait_until_pending(timeout=1))
        interaction = events[-1]
        self.assertEqual(interaction["type"], "interaction")
        self.assertEqual(interaction["kind"], "approval")

        self.assertTrue(provider.resolve(interaction["id"], {"decision": "approve"}))
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result_holder[0].approved)

    def test_interaction_provider_returns_selected_question_option(self):
        events = []
        provider = BridgeInteractionProvider(events.append, project_root=Path.cwd())
        request = QuestionRequest(
            question="Which framework?",
            options=[QuestionOption("React", "react"), QuestionOption("Other", "other", is_other=True)],
        )
        result_holder = []

        worker = threading.Thread(target=lambda: result_holder.append(provider.ask(request)))
        worker.start()
        self.assertTrue(provider.wait_until_pending(timeout=1))
        interaction = events[-1]
        provider.resolve(interaction["id"], {"selectedIndex": 1, "customText": "OpenTUI"})
        worker.join(timeout=1)

        self.assertEqual(result_holder[0].selected_index, 1)
        self.assertEqual(result_holder[0].custom_text, "OpenTUI")

    def test_event_translation_and_request_response(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "harness_code_agent.tui_bridge.InteractiveSession", _FakeSession
        ), patch("harness_code_agent.tui_bridge.sys.stdout", output):
            server = BridgeServer(cwd=Path(tmp), profile_name="general", profile_explicit=False)
            server._handle_request({"type": "request", "id": "init", "method": "initialize"})
            server._run_task("inspect parser", CancellationToken())
            server.close()

        messages = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertTrue(
            any(
                message.get("type") == "response"
                and message.get("id") == "init"
                and message.get("ok")
                for message in messages
            )
        )
        events = [message["event"] for message in messages if message.get("type") == "event"]
        self.assertTrue(any(event.get("type") == "assistant_delta" and event.get("text") == "hello" for event in events))
        self.assertTrue(any(event.get("type") == "transcript_update" and event.get("state") == "success" for event in events))
        self.assertTrue(any(event.get("type") == "shutdown" for event in events))

    def test_submit_shows_assistant_before_first_provider_delta(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "harness_code_agent.tui_bridge.InteractiveSession", _SlowFirstDeltaSession
        ), patch("harness_code_agent.tui_bridge.sys.stdout", output):
            server = BridgeServer(cwd=Path(tmp), profile_name="general", profile_explicit=False)
            server._handle_request({
                "type": "request",
                "id": "submit",
                "method": "submit",
                "params": {"text": "slow response"},
            })
            self.assertTrue(_SlowFirstDeltaSession.started.wait(timeout=1))
            events = [
                json.loads(line)["event"]
                for line in output.getvalue().splitlines()
                if line.strip() and json.loads(line).get("type") == "event"
            ]
            self.assertTrue(any(
                event.get("type") == "transcript"
                and event.get("item", {}).get("kind") == "assistant"
                and event.get("item", {}).get("state") == "running"
                for event in events
            ))
            _SlowFirstDeltaSession.release.set()
            server._tasks.join()
            server.close()

    def test_cancel_clears_queued_submissions(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "harness_code_agent.tui_bridge.InteractiveSession", _SlowFirstDeltaSession
        ), patch("harness_code_agent.tui_bridge.sys.stdout", output):
            server = BridgeServer(cwd=Path(tmp), profile_name="general", profile_explicit=False)
            server._handle_request({
                "type": "request",
                "id": "first",
                "method": "submit",
                "params": {"text": "first"},
            })
            self.assertTrue(_SlowFirstDeltaSession.started.wait(timeout=1))
            server._handle_request({
                "type": "request",
                "id": "second",
                "method": "submit",
                "params": {"text": "queued"},
            })
            server._handle_request({"type": "request", "id": "cancel", "method": "cancel"})
            _SlowFirstDeltaSession.release.set()
            server._tasks.join()
            server.close()

        messages = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        cancel_response = next(message for message in messages if message.get("id") == "cancel")
        self.assertEqual(cancel_response["result"]["discardedQueued"], 1)
        assistant_updates = [
            message["event"] for message in messages
            if message.get("type") == "event"
            and message.get("event", {}).get("type") == "transcript_update"
        ]
        self.assertTrue(any(event.get("state") == "failed" for event in assistant_updates))

    def test_structured_action_returns_searchable_session_panel(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "harness_code_agent.tui_bridge.InteractiveSession", _FakeSession
        ), patch("harness_code_agent.tui_bridge.sys.stdout", output):
            server = BridgeServer(cwd=Path(tmp), profile_name="general", profile_explicit=False)
            server._handle_request({
                "type": "request",
                "id": "sessions",
                "method": "action",
                "params": {"name": "open_sessions", "params": {}},
            })
            server.close()

        responses = [json.loads(line) for line in output.getvalue().splitlines() if '"id":"sessions"' in line]
        panel = responses[0]["result"]["panel"]
        self.assertEqual(panel["kind"], "sessions")
        self.assertTrue(panel["searchable"])
        self.assertEqual(panel["options"][0]["label"], "inspect parser")


if __name__ == "__main__":
    unittest.main()
