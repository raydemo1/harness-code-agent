import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path

from harness_code_agent import config
from harness_code_agent.workspace.shell_session import (
    PersistentShellSession,
    _DockerShellBackend,
    _docker_user_arg,
    docker_shell_hint,
    sandbox_mode,
    windows_shell_kind,
)


def _commands_for_platform(temp_dir: str) -> dict[str, str]:
    if os.name == "nt":
        if windows_shell_kind() in {"pwsh", "powershell"}:
            return {
                "make_and_cd": "Set-Location ..",
                "pwd": "(Get-Location).Path",
                "pwd_expected": str(Path(temp_dir).resolve().parent),
                "set_env": "$env:FOO='bar'",
                "get_env": "$env:FOO",
                "sleep": "Start-Sleep -Seconds 5",
                "alive": "'alive'",
            }
        return {
            "make_and_cd": "cd ..",
            "pwd": "cd",
            "pwd_expected": str(Path(temp_dir).resolve().parent),
            "set_env": "set FOO=bar",
            "get_env": "echo %FOO%",
            "sleep": "powershell -NoLogo -NoProfile -Command \"Start-Sleep -Seconds 5\"",
            "alive": "echo alive",
        }
    return {
        "make_and_cd": "mkdir -p subdir && cd subdir",
        "pwd": "pwd",
        "pwd_expected": os.path.abspath(os.path.join(temp_dir, "subdir")),
        "set_env": "export FOO=bar",
        "get_env": "printf '%s' \"$FOO\"",
        "sleep": "sleep 5",
        "alive": "echo alive",
    }


class PersistentShellSessionTests(unittest.TestCase):
    def _make_temp_dir(self) -> str:
        return tempfile.mkdtemp(dir=os.getcwd())

    def test_commands_for_platform_returns_posix_commands(self):
        temp_dir = self._make_temp_dir()
        try:
            with patch("os.name", "posix"):
                commands = _commands_for_platform(temp_dir)

            self.assertEqual(commands["make_and_cd"], "mkdir -p subdir && cd subdir")
            self.assertEqual(commands["pwd"], "pwd")
            self.assertEqual(commands["set_env"], "export FOO=bar")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_shell_preserves_working_directory(self):
        temp_dir = self._make_temp_dir()
        commands = _commands_for_platform(temp_dir)
        shell = PersistentShellSession(cwd=temp_dir)
        try:
            shell.run(commands["make_and_cd"])
            result = shell.run(commands["pwd"])

            if os.name != "nt":
                self.assertEqual(result.exit_code, 0)
            self.assertEqual(Path(result.stdout.strip()).resolve(), Path(commands["pwd_expected"]).resolve())
        finally:
            shell.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_shell_preserves_environment_variables(self):
        temp_dir = self._make_temp_dir()
        commands = _commands_for_platform(temp_dir)
        shell = PersistentShellSession(cwd=temp_dir)
        try:
            shell.run(commands["set_env"])
            result = shell.run(commands["get_env"])

            if os.name != "nt":
                self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "bar")
        finally:
            shell.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_shell_timeout_interrupts_command_but_keeps_session_alive(self):
        temp_dir = self._make_temp_dir()
        commands = _commands_for_platform(temp_dir)
        shell = PersistentShellSession(cwd=temp_dir)
        try:
            timed_out = shell.run(commands["sleep"], timeout=1)
            follow_up = shell.run(commands["alive"])

            self.assertTrue(timed_out.timed_out)
            self.assertIn("alive", follow_up.stdout)
            if os.name != "nt":
                self.assertEqual(follow_up.exit_code, 0)
        finally:
            shell.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_windows_shell_auto_prefers_pwsh_then_powershell_then_cmd(self):
        if os.name != "nt":
            self.skipTest("Windows-only shell selection")
        from harness_code_agent.workspace import shell_session

        def fake_which(name):
            return {
                "pwsh": "C:/bin/pwsh.exe",
                "powershell": "C:/bin/powershell.exe",
                "cmd.exe": "C:/Windows/System32/cmd.exe",
            }.get(name)

        with (
            patch.object(config, "WINDOWS_SHELL", "auto"),
            patch("harness_code_agent.workspace.shell_session.shutil.which", side_effect=fake_which),
        ):
            self.assertEqual(shell_session.windows_shell_path(), "C:/bin/pwsh.exe")
            self.assertEqual(shell_session.windows_shell_kind(), "pwsh")

        def fake_which_without_pwsh(name):
            return {
                "powershell": "C:/bin/powershell.exe",
                "cmd.exe": "C:/Windows/System32/cmd.exe",
            }.get(name)

        with (
            patch.object(config, "WINDOWS_SHELL", "auto"),
            patch("harness_code_agent.workspace.shell_session.shutil.which", side_effect=fake_which_without_pwsh),
        ):
            self.assertEqual(shell_session.windows_shell_path(), "C:/bin/powershell.exe")
            self.assertEqual(shell_session.windows_shell_kind(), "powershell")

    def test_windows_powershell_backend_preserves_utf8_output(self):
        if os.name != "nt" or windows_shell_kind() not in {"pwsh", "powershell"}:
            self.skipTest("PowerShell backend not active")
        temp_dir = self._make_temp_dir()
        shell = PersistentShellSession(cwd=temp_dir)
        try:
            result = shell.run("'中文 output'")

            self.assertEqual(result.exit_code, 0)
            self.assertIn("中文 output", result.stdout)
        finally:
            shell.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_docker_sandbox_mode_uses_docker_backend(self):
        from harness_code_agent.workspace import shell_session

        temp_dir = self._make_temp_dir()
        try:
            with (
                patch.object(config, "SANDBOX_MODE", "docker"),
                patch("harness_code_agent.workspace.shell_session._DockerShellBackend") as docker_backend,
            ):
                shell = PersistentShellSession(cwd=temp_dir)

            docker_backend.assert_called_once_with(str(Path(temp_dir).resolve()))
            self.assertIs(shell._backend, docker_backend.return_value)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_docker_run_args_mount_workspace_read_write_with_offline_network(self):
        from harness_code_agent.workspace import shell_session

        temp_dir = self._make_temp_dir()
        try:
            with (
                patch.object(config, "DOCKER_IMAGE", "python:3.12"),
                patch.object(config, "DOCKER_NETWORK", "none"),
                patch.object(config, "DOCKER_USER", ""),
                patch("harness_code_agent.workspace.shell_session._docker_user_arg", return_value=[]),
            ):
                args = shell_session._docker_run_args("hca-test", str(Path(temp_dir).resolve()))

            self.assertEqual(args[:4], ["docker", "run", "-d", "--name"])
            self.assertIn("hca-test", args)
            self.assertIn("--network", args)
            self.assertIn("none", args)
            self.assertIn("--security-opt=no-new-privileges", args)
            self.assertIn("-v", args)
            self.assertIn(f"{Path(temp_dir).resolve()}:/workspace", args)
            self.assertIn("-w", args)
            self.assertIn("/workspace", args)
            self.assertEqual(args[-3:], ["python:3.12", "sleep", "infinity"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_docker_run_args_explicit_user(self):
        from harness_code_agent.workspace import shell_session

        temp_dir = self._make_temp_dir()
        try:
            with (
                patch.object(config, "DOCKER_IMAGE", "python:3.12"),
                patch.object(config, "DOCKER_NETWORK", "none"),
                patch.object(config, "DOCKER_USER", "1000:1000"),
            ):
                args = shell_session._docker_run_args("hca-test", str(Path(temp_dir).resolve()))

            self.assertIn("--user", args)
            user_idx = args.index("--user")
            self.assertEqual(args[user_idx + 1], "1000:1000")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_docker_user_arg_auto_posix_non_root(self):
        with (
            patch.object(config, "DOCKER_USER", ""),
            patch("os.name", "posix"),
            patch("os.getuid", return_value=1000, create=True),
            patch("os.getgid", return_value=1000, create=True),
        ):
            result = _docker_user_arg()
            self.assertEqual(result, ["--user", "1000:1000"])

    def test_docker_user_arg_no_mapping_for_root(self):
        with (
            patch.object(config, "DOCKER_USER", ""),
            patch("os.name", "posix"),
            patch("os.getuid", return_value=0, create=True),
            patch("os.getgid", return_value=0, create=True),
        ):
            result = _docker_user_arg()
            self.assertEqual(result, [])

    def test_docker_user_arg_maps_non_root_uid_even_with_root_gid(self):
        with (
            patch.object(config, "DOCKER_USER", ""),
            patch("os.name", "posix"),
            patch("os.getuid", return_value=1000, create=True),
            patch("os.getgid", return_value=0, create=True),
        ):
            result = _docker_user_arg()
            self.assertEqual(result, ["--user", "1000:0"])

    def test_docker_user_arg_no_mapping_on_windows(self):
        with (
            patch.object(config, "DOCKER_USER", ""),
            patch("os.name", "nt"),
        ):
            result = _docker_user_arg()
            self.assertEqual(result, [])

    def test_docker_user_arg_no_uid_available(self):
        with (
            patch.object(config, "DOCKER_USER", ""),
            patch("os.name", "posix"),
            patch("os.getuid", side_effect=AttributeError, create=True),
        ):
            result = _docker_user_arg()
            self.assertEqual(result, [])

    def test_sandbox_mode_invalid_value_raises(self):
        with patch.object(config, "SANDBOX_MODE", "lxc"):
            with self.assertRaises(ValueError):
                sandbox_mode()

    def test_docker_shell_hint_host_mode(self):
        with patch.object(config, "SANDBOX_MODE", "host"):
            self.assertEqual(docker_shell_hint(), "host shell")

    def test_docker_shell_hint_cli_missing(self):
        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch("harness_code_agent.workspace.shell_session.docker_cli_path", return_value=None),
        ):
            self.assertEqual(docker_shell_hint(), "Docker CLI not found")

    def test_docker_shell_hint_docker_available(self):
        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch.object(config, "DOCKER_IMAGE", "python:3.12"),
            patch.object(config, "DOCKER_NETWORK", "none"),
            patch("harness_code_agent.workspace.shell_session.docker_cli_path", return_value="/usr/bin/docker"),
        ):
            hint = docker_shell_hint()
            self.assertIn("Docker sandbox", hint)
            self.assertIn("python:3.12", hint)

    def test_docker_shell_hint_includes_user_when_set(self):
        with (
            patch.object(config, "SANDBOX_MODE", "docker"),
            patch.object(config, "DOCKER_IMAGE", "python:3.12"),
            patch.object(config, "DOCKER_NETWORK", "none"),
            patch.object(config, "DOCKER_USER", "1000:1000"),
            patch("harness_code_agent.workspace.shell_session.docker_cli_path", return_value="/usr/bin/docker"),
        ):
            hint = docker_shell_hint()
            self.assertIn("user=1000:1000", hint)

    def test_docker_cleanup_swallows_docker_rm_errors(self):
        import subprocess as sp

        backend = _DockerShellBackend.__new__(_DockerShellBackend)
        backend.container_name = "hca-test-xyz"
        backend._container_started = True

        def fake_run(*args, **kwargs):
            raise Exception("docker rm failed")

        with patch("harness_code_agent.workspace.shell_session.subprocess.run", side_effect=fake_run):
            # Should not raise
            backend._cleanup_impl()
            self.assertFalse(backend._container_started)

    def test_stop_exec_process_waits_for_reader_thread(self):
        import threading

        backend = _DockerShellBackend.__new__(_DockerShellBackend)
        # Simulate a stopped process
        proc = unittest.mock.MagicMock()
        proc.poll.return_value = 0
        proc.stdin = unittest.mock.MagicMock()
        proc.stdout = unittest.mock.MagicMock()
        backend._process = proc

        reader_started = threading.Event()
        reader_exited = threading.Event()

        def reader_run():
            reader_started.set()
            reader_exited.wait()  # block until told to exit

        reader = threading.Thread(target=reader_run)
        reader.start()
        reader_started.wait()
        backend._reader = reader

        backend._stop_exec_process(timeout=2)

        reader_exited.set()
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())

    def test_docker_backend_interrupt_does_not_set_closed(self):
        backend = _DockerShellBackend.__new__(_DockerShellBackend)
        backend._closed = False
        backend.container_name = "hca-test-interrupt"
        backend._container_started = False
        backend._reader = unittest.mock.MagicMock()
        backend._reader.is_alive.return_value = False
        backend._queue = unittest.mock.MagicMock()
        backend._process = unittest.mock.MagicMock()
        backend._process.poll.return_value = 0
        backend._process.stdin = unittest.mock.MagicMock()
        backend._process.stdout = unittest.mock.MagicMock()

        with (
            patch.object(backend, "_interrupt_impl") as mock_interrupt,
            patch.object(backend, "_sync") as mock_sync,
        ):
            backend.interrupt()
            mock_interrupt.assert_called_once()
            mock_sync.assert_called_once()

        # interrupt() should NOT set _closed
        self.assertFalse(backend._closed)

    def test_docker_backend_close_sets_closed(self):
        import queue

        backend = _DockerShellBackend.__new__(_DockerShellBackend)
        backend._closed = False
        backend.container_name = "hca-test-close"
        backend._container_started = False
        backend._reader = unittest.mock.MagicMock()
        backend._queue = unittest.mock.MagicMock()
        backend._queue.get_nowait.side_effect = queue.Empty

        with patch.object(backend, "_cleanup_impl"):
            backend.close()
            self.assertTrue(backend._closed)

        # Second close is a no-op
        with patch.object(backend, "_cleanup_impl") as mock_cleanup:
            backend.close()
            mock_cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
