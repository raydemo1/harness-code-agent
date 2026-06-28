import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from eval.benchmarks.run_terminal_bench import (
    build_harbor_run_command,
    build_launch_environment,
    default_local_dataset_path,
    docker_daemon_running,
    ensure_local_dataset,
    is_valid_harbor_dataset,
    main,
    repair_task_images,
    resolve_harbor_dataset_path,
    resolve_harbor_executable,
)
from eval.benchmarks.harbor_env import runner_env_vars


class TerminalBenchLauncherTests(unittest.TestCase):
    def _workspace_path(self, name: str) -> Path:
        path = Path(os.getcwd()) / "workspace" / f"{name}-{uuid.uuid4().hex}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_build_command_uses_current_harbor_task_filter_flag(self):
        command = build_harbor_run_command(
            harbor_executable="harbor",
            tasks=["fix-git", "headless-terminal"],
            runner_env=None,
            dataset_path=None,
            force_build=False,
        )

        self.assertEqual(
            command,
            [
                "harbor",
                "run",
                "-d",
                "terminal-bench@2.1",
                "--agent-import-path",
                "eval.benchmarks.harbor_agent:HarnessAgent",
                "--include-task-name",
                "fix-git",
                "--include-task-name",
                "headless-terminal",
            ],
        )

    def test_build_command_can_target_local_dataset_path(self):
        command = build_harbor_run_command(
            harbor_executable="harbor",
            tasks=["fix-git"],
            runner_env="daytona",
            dataset_path=Path("E:/tmp/terminal-bench-2-1"),
            force_build=False,
        )

        self.assertEqual(
            command,
            [
                "harbor",
                "run",
                "--path",
                "E:\\tmp\\terminal-bench-2-1",
                "--agent-import-path",
                "eval.benchmarks.harbor_agent:HarnessAgent",
                "--env",
                "daytona",
                "--include-task-name",
                "fix-git",
            ],
        )

    def test_build_command_can_force_environment_build(self):
        command = build_harbor_run_command(
            harbor_executable="harbor",
            tasks=["fix-git"],
            runner_env=None,
            dataset_path=Path("E:/tmp/terminal-bench-2-1"),
            force_build=True,
        )

        self.assertEqual(
            command,
            [
                "harbor",
                "run",
                "--path",
                "E:\\tmp\\terminal-bench-2-1",
                "--agent-import-path",
                "eval.benchmarks.harbor_agent:HarnessAgent",
                "--force-build",
                "--include-task-name",
                "fix-git",
            ],
        )

    def test_build_environment_uses_repo_local_temp_and_loads_missing_dotenv_values(self):
        repo_root = self._workspace_path("test-terminal-bench-launcher-env")
        try:
            dotenv_path = repo_root / ".env"
            dotenv_path.write_text(
                "OPENAI_API_KEY=test-key\nHARNESS_MODEL=dotenv-model\n",
                encoding="utf-8",
            )
            base_env = {
                "PATH": "base-path",
                "HARNESS_MODEL": "existing-model",
            }

            env = build_launch_environment(repo_root, base_env=base_env, dotenv_path=dotenv_path)

            expected_temp = str((repo_root / ".harbor" / "tmp").resolve())
            self.assertEqual(env["TEMP"], expected_temp)
            self.assertEqual(env["TMP"], expected_temp)
            self.assertEqual(env["OPENAI_API_KEY"], "test-key")
            self.assertEqual(env["HARNESS_MODEL"], "existing-model")
            self.assertEqual(env["MAX_AGENT_ITERATIONS"], "100")
            self.assertEqual(env["MAX_AGENT_TOOL_CALLS"], "400")
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(env["PYTHONUTF8"], "1")
            self.assertEqual(env["NO_COLOR"], "1")
            self.assertEqual(env["TERM"], "dumb")
            self.assertEqual(env["RICH_FORCE_TERMINAL"], "0")
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_build_environment_preserves_explicit_agent_budget(self):
        repo_root = self._workspace_path("test-terminal-bench-launcher-budget")
        try:
            env = build_launch_environment(
                repo_root,
                base_env={
                    "PATH": "base-path",
                    "MAX_AGENT_ITERATIONS": "120",
                    "MAX_AGENT_TOOL_CALLS": "500",
                },
            )

            self.assertEqual(env["MAX_AGENT_ITERATIONS"], "120")
            self.assertEqual(env["MAX_AGENT_TOOL_CALLS"], "500")
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_harbor_agent_forwards_agent_budget_environment(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "secret",
                "HARNESS_MODEL": "deepseek-v4-flash",
                "MAX_AGENT_ITERATIONS": "100",
                "MAX_AGENT_TOTAL_TOKENS": "900000",
                "MAX_AGENT_TOOL_CALLS": "400",
                "AGENT_BUDGET_WARN_FRACTION": "0.9",
            },
            clear=True,
        ):
            env = runner_env_vars()

        self.assertEqual(env["MAX_AGENT_ITERATIONS"], "100")
        self.assertEqual(env["MAX_AGENT_TOTAL_TOKENS"], "900000")
        self.assertEqual(env["MAX_AGENT_TOOL_CALLS"], "400")
        self.assertEqual(env["AGENT_BUDGET_WARN_FRACTION"], "0.9")
        self.assertEqual(env["HARNESS_MODEL"], "deepseek-v4-flash")

    def test_docker_daemon_running_returns_false_on_cli_failure(self):
        with patch("eval.benchmarks.run_terminal_bench.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1

            self.assertFalse(docker_daemon_running())

    def test_main_fails_fast_when_docker_daemon_is_unavailable(self):
        repo_root = self._workspace_path("test-terminal-bench-docker-preflight")
        dataset_path = repo_root / "dataset"
        task_dir = dataset_path / "tasks" / "overfull-hbox"
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text("name='overfull-hbox'\n", encoding="utf-8")
        try:
            with (
                patch("eval.benchmarks.run_terminal_bench.resolve_harbor_executable", return_value="harbor"),
                patch("eval.benchmarks.run_terminal_bench.repair_task_images", return_value=0),
                patch("eval.benchmarks.run_terminal_bench.docker_daemon_running", return_value=False),
                patch("eval.benchmarks.run_terminal_bench.subprocess.run") as run_mock,
                patch("sys.argv", ["run_terminal_bench.py", "--task", "overfull-hbox", "--dataset-path", str(dataset_path)]),
            ):
                result = main()

            self.assertEqual(result, 125)
            run_mock.assert_not_called()
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_resolve_harbor_executable_falls_back_to_user_scripts(self):
        repo_root = self._workspace_path("test-terminal-bench-launcher-bin")
        try:
            appdata = repo_root / "AppData" / "Roaming"
            scripts_dir = appdata / "Python" / "Python312" / "Scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            harbor_path = scripts_dir / "harbor.exe"
            harbor_path.write_text("", encoding="utf-8")

            with patch("eval.benchmarks.run_terminal_bench.shutil.which", return_value=None):
                resolved = resolve_harbor_executable(
                    {
                        "APPDATA": str(appdata),
                        "USERPROFILE": str(repo_root / "UserProfile"),
                    }
                )

            self.assertEqual(resolved, str(harbor_path))
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_default_local_dataset_path_is_repo_scoped(self):
        repo_root = Path("E:/repo-root")

        resolved = default_local_dataset_path(repo_root)

        self.assertEqual(
            resolved,
            (repo_root / ".harbor" / "datasets" / "terminal-bench-2-1").resolve(),
        )

    def test_is_valid_harbor_dataset_requires_2_1_tasks_layout(self):
        repo_root = self._workspace_path("test-terminal-bench-validity")
        try:
            invalid_dataset = repo_root / "terminal-bench-2-1"
            invalid_dataset.mkdir(parents=True, exist_ok=True)
            (invalid_dataset / ".git").mkdir(exist_ok=True)

            self.assertFalse(is_valid_harbor_dataset(invalid_dataset))

            valid_task_dir = invalid_dataset / "fix-git"
            valid_task_dir.mkdir(parents=True, exist_ok=True)
            (valid_task_dir / "task.toml").write_text("name='fix-git'\n", encoding="utf-8")

            self.assertFalse(is_valid_harbor_dataset(invalid_dataset))

            task_dir = invalid_dataset / "tasks" / "fix-git"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.toml").write_text("name='fix-git'\n", encoding="utf-8")
            self.assertTrue(is_valid_harbor_dataset(invalid_dataset))
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_resolve_harbor_dataset_path_returns_2_1_tasks_directory(self):
        repo_root = self._workspace_path("test-terminal-bench-harbor-path")
        try:
            dataset_path = repo_root / "terminal-bench-2-1"
            task_dir = dataset_path / "tasks" / "fix-git"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.toml").write_text("name='fix-git'\n", encoding="utf-8")

            self.assertEqual(resolve_harbor_dataset_path(dataset_path), dataset_path / "tasks")
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_resolve_harbor_dataset_path_rejects_legacy_root_task_layout(self):
        repo_root = self._workspace_path("test-terminal-bench-harbor-path-legacy")
        try:
            dataset_path = repo_root / "terminal-bench-2-1"
            task_dir = dataset_path / "fix-git"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.toml").write_text("name='fix-git'\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "tasks/\\*/task.toml"):
                resolve_harbor_dataset_path(dataset_path)
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_ensure_local_dataset_reclones_invalid_partial_checkout(self):
        repo_root = self._workspace_path("test-terminal-bench-reclone")
        dataset_path = repo_root / ".harbor" / "datasets" / "terminal-bench-2-1"
        dataset_path.mkdir(parents=True, exist_ok=True)
        downloaded_paths = []

        def fake_download(path):
            downloaded_paths.append(path)
            task_dir = dataset_path / "tasks" / "fix-git"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "task.toml").write_text("name='fix-git'\n", encoding="utf-8")

        try:
            with patch("eval.benchmarks.run_terminal_bench.shutil.rmtree") as rmtree_mock, patch(
                "eval.benchmarks.run_terminal_bench._download_and_extract_dataset_archive",
                side_effect=fake_download,
            ) as download_mock:
                resolved = ensure_local_dataset(dataset_path)

            self.assertEqual(resolved, dataset_path.resolve())
            self.assertTrue(is_valid_harbor_dataset(dataset_path))
            self.assertEqual(download_mock.call_count, 1)
            self.assertEqual(rmtree_mock.call_count, 1)
            self.assertEqual(downloaded_paths, [dataset_path.resolve()])
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_repair_task_images_rewrites_unavailable_image_to_dockerhub_fallback(self):
        repo_root = self._workspace_path("test-terminal-bench-image-repair")
        dataset_path = repo_root / ".harbor" / "datasets" / "terminal-bench-2-1"
        try:
            task_dir = dataset_path / "tasks" / "overfull-hbox"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / "task.toml"
            task_file.write_text(
                "\n".join(
                    [
                        'version = "1.0"',
                        "",
                        "[environment]",
                        'docker_image = "ghcr.io/laude-institute/terminal-bench/overfull-hbox:2.0"',
                    ]
                ),
                encoding="utf-8",
            )

            def fake_exists(image: str) -> bool:
                return image == "alexgshaw/overfull-hbox:20251031"

            with patch("eval.benchmarks.run_terminal_bench._docker_image_exists", side_effect=fake_exists):
                rewritten = repair_task_images(dataset_path)

            self.assertEqual(rewritten, 1)
            self.assertIn(
                'docker_image = "alexgshaw/overfull-hbox:20251031"',
                task_file.read_text(encoding="utf-8"),
            )
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_repair_task_images_preserves_available_image(self):
        repo_root = self._workspace_path("test-terminal-bench-image-preserve")
        dataset_path = repo_root / ".harbor" / "datasets" / "terminal-bench-2-1"
        try:
            task_dir = dataset_path / "tasks" / "fix-git"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / "task.toml"
            task_file.write_text(
                "\n".join(
                    [
                        "[environment]",
                        'docker_image = "ghcr.io/laude-institute/terminal-bench/fix-git:2.0"',
                    ]
                ),
                encoding="utf-8",
            )

            with patch("eval.benchmarks.run_terminal_bench._docker_image_exists", return_value=True):
                rewritten = repair_task_images(dataset_path)

            self.assertEqual(rewritten, 0)
            self.assertIn(
                'docker_image = "ghcr.io/laude-institute/terminal-bench/fix-git:2.0"',
                task_file.read_text(encoding="utf-8"),
            )
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_repair_task_images_can_limit_to_selected_tasks(self):
        repo_root = self._workspace_path("test-terminal-bench-image-filter")
        dataset_path = repo_root / ".harbor" / "datasets" / "terminal-bench-2-1"
        try:
            for task in ("overfull-hbox", "custom-memory-heap-crash"):
                task_dir = dataset_path / "tasks" / task
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "task.toml").write_text(
                    "\n".join(
                        [
                            "[environment]",
                            f'docker_image = "ghcr.io/laude-institute/terminal-bench/{task}:2.0"',
                        ]
                    ),
                    encoding="utf-8",
                )

            seen: list[str] = []

            def fake_exists(image: str) -> bool:
                seen.append(image)
                return image == "alexgshaw/overfull-hbox:20251031"

            with patch("eval.benchmarks.run_terminal_bench._docker_image_exists", side_effect=fake_exists):
                repaired = repair_task_images(dataset_path, ["overfull-hbox"])

            self.assertEqual(repaired, 1)
            self.assertIn("overfull-hbox", "\n".join(seen))
            self.assertNotIn("custom-memory-heap-crash", "\n".join(seen))
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    def test_repair_task_images_uses_2_1_tasks_layout(self):
        repo_root = self._workspace_path("test-terminal-bench-image-tasks-dir")
        dataset_path = repo_root / ".harbor" / "datasets" / "terminal-bench-2-1"
        try:
            task_dir = dataset_path / "tasks" / "overfull-hbox"
            task_dir.mkdir(parents=True, exist_ok=True)
            task_file = task_dir / "task.toml"
            task_file.write_text(
                "\n".join(
                    [
                        "[environment]",
                        'docker_image = "ghcr.io/laude-institute/terminal-bench/overfull-hbox:2.1"',
                    ]
                ),
                encoding="utf-8",
            )

            def fake_exists(image: str) -> bool:
                return image == "alexgshaw/overfull-hbox:20251031"

            with patch("eval.benchmarks.run_terminal_bench._docker_image_exists", side_effect=fake_exists):
                repaired = repair_task_images(dataset_path, ["overfull-hbox"])

            self.assertEqual(repaired, 1)
            self.assertIn(
                'docker_image = "alexgshaw/overfull-hbox:20251031"',
                task_file.read_text(encoding="utf-8"),
            )
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
