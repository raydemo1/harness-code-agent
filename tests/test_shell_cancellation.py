from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness_code_agent.agent.cancellation import CancellationToken, CancelledError
from harness_code_agent.runtime.builtins.registry import BUILTIN_TOOL_REGISTRY
from harness_code_agent.runtime.builtins.shell import run_bash
from harness_code_agent.runtime.tool_runner import execute_tool_result
from harness_code_agent.workspace.shell_session import ShellResult


class _BlockingShellSession:
    def __init__(self, _cwd: str) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.interrupted = False
        self.closed = False

    def run(self, _command: str, timeout: int = 300) -> ShellResult:
        self.started.set()
        self.released.wait(timeout=timeout)
        return ShellResult("", "", 130)

    def interrupt(self) -> None:
        self.interrupted = True
        self.released.set()

    def close(self) -> None:
        self.closed = True


class ShellCancellationTests(unittest.TestCase):
    def test_foreground_shell_is_interrupted_and_unregistered_on_cancel(self):
        token = CancellationToken()
        shell = _BlockingShellSession(".")
        registered: list[object] = []
        unregistered: list[object] = []
        runtime_state = SimpleNamespace(
            register_shell_session=registered.append,
            unregister_shell_session=unregistered.append,
        )
        tool_context = SimpleNamespace(workspace=SimpleNamespace(root=Path.cwd()))
        errors: list[Exception] = []

        def invoke() -> None:
            try:
                run_bash(
                    "long command",
                    runtime_state=runtime_state,
                    tool_context=tool_context,
                    cancellation_token=token,
                )
            except Exception as exc:  # noqa: BLE001 - assertion captures worker failure
                errors.append(exc)

        with patch(
            "harness_code_agent.workspace.shell_session.PersistentShellSession",
            return_value=shell,
        ):
            worker = threading.Thread(target=invoke)
            worker.start()
            self.assertTrue(shell.started.wait(timeout=1))
            token.cancel()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(shell.interrupted)
        self.assertTrue(shell.closed)
        self.assertEqual(registered, [shell])
        self.assertEqual(unregistered, [shell])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CancelledError)

    def test_tool_runner_does_not_convert_cancellation_into_a_failed_result(self):
        token = CancellationToken()
        token.cancel()

        def cancel_tool(cancellation_token=None):
            cancellation_token.check()

        with (
            patch.object(BUILTIN_TOOL_REGISTRY, "get", return_value=cancel_tool),
            self.assertRaises(CancelledError),
        ):
            execute_tool_result("cancel_tool", {}, cancellation_token=token)


if __name__ == "__main__":
    unittest.main()
