from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness_code_agent.tui.protocol import UI_PROTOCOL_VERSION, validate_ui_event
from harness_code_agent.tui_bridge import BridgeServer


class TuiProtocolTests(unittest.TestCase):
    def _server(self) -> BridgeServer:
        server = object.__new__(BridgeServer)
        server.cwd = Path("workspace")
        server._session = SimpleNamespace()
        server._session_error = None
        server._session_thread = SimpleNamespace(is_alive=lambda: False)
        server.responses = []
        server._response = lambda request_id, result=None, error=None: server.responses.append(
            {"id": request_id, "result": result, "error": error}
        )
        return server

    def test_initialize_requires_the_shared_protocol_version(self):
        server = self._server()

        BridgeServer._handle_request(server, {
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": UI_PROTOCOL_VERSION},
        })

        self.assertEqual(
            server.responses[0]["result"]["protocolVersion"],
            UI_PROTOCOL_VERSION,
        )
        self.assertIsNone(server.responses[0]["error"])

    def test_initialize_rejects_a_mismatched_protocol_version(self):
        server = self._server()

        BridgeServer._handle_request(server, {
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": UI_PROTOCOL_VERSION + 1},
        })

        self.assertIsNone(server.responses[0]["result"])
        self.assertIn("版本不兼容", server.responses[0]["error"])

    def test_event_validation_rejects_unknown_and_incomplete_events(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_ui_event({"type": "surprise"})
        with self.assertRaisesRegex(ValueError, "detail"):
            validate_ui_event({"type": "progress", "status": "starting"})

    def test_stale_session_construction_closes_without_publishing(self):
        server = object.__new__(BridgeServer)
        server.cwd = Path("workspace")
        server.profile_name = "general"
        server.profile_explicit = False
        server._interactions = SimpleNamespace()
        server._session_lock = threading.Lock()
        server._session_generation = 2
        server._closing = threading.Event()
        server._session = None
        server._session_error = None
        closed: list[bool] = []
        stale_session = SimpleNamespace(close=lambda: closed.append(True))

        with patch(
            "harness_code_agent.tui_bridge.InteractiveSession",
            return_value=stale_session,
        ):
            server._construct_session(generation=1)

        self.assertEqual(closed, [True])
        self.assertIsNone(server._session)


if __name__ == "__main__":
    unittest.main()
