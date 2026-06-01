import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path

from harness_code_agent import config
from harness_code_agent.workspace.shell_session import PersistentShellSession, windows_shell_kind


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
            "pwd_expected": str(Path(temp_dir, "subdir").resolve()),
            "set_env": "export FOO=bar",
            "get_env": "printf '%s' \"$FOO\"",
            "sleep": "sleep 5",
            "alive": "echo alive",
        }


class PersistentShellSessionTests(unittest.TestCase):
    def _make_temp_dir(self) -> str:
        return tempfile.mkdtemp(dir=os.getcwd())

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
            ):
                args = shell_session._docker_run_args("hca-test", str(Path(temp_dir).resolve()))

            self.assertEqual(args[:4], ["docker", "run", "-d", "--name"])
            self.assertIn("hca-test", args)
            self.assertIn("--network", args)
            self.assertIn("none", args)
            self.assertIn("-v", args)
            self.assertIn(f"{Path(temp_dir).resolve()}:/workspace", args)
            self.assertIn("-w", args)
            self.assertIn("/workspace", args)
            self.assertEqual(args[-3:], ["python:3.12", "sleep", "infinity"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


