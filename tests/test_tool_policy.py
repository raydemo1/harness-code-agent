from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_code_agent import config
from harness_code_agent.agent.runtime_state import AgentRuntimeState
from harness_code_agent.runtime.builtins.filesystem import list_files, read_file, repo_search
from harness_code_agent.runtime.middleware.tool_policy import ToolPolicyMiddleware


class RepositoryToolPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_workspace = config.WORKSPACE
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        config.WORKSPACE = str(self.workspace)
        (self.workspace / "pkg").mkdir()
        (self.workspace / "pkg" / "target.py").write_text("VALUE = 'needle'\n", encoding="utf-8")
        (self.workspace / ".harness" / "observations" / "s").mkdir(parents=True)
        (self.workspace / ".harness" / "observations" / "s" / "obs.txt").write_text("secret", encoding="utf-8")
        (self.workspace / "pkg" / "__pycache__").mkdir()
        (self.workspace / "pkg" / "__pycache__" / "ignored.pyc").write_text("needle", encoding="utf-8")

    def tearDown(self) -> None:
        config.WORKSPACE = self._old_workspace
        self._tmp.cleanup()

    def test_repo_search_uses_bounded_explicit_path_and_excludes_generated_dirs(self) -> None:
        result = repo_search("needle", path=".", max_results=5)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.metadata["explicit_path"], ".")
        self.assertIn("pkg/target.py", result.output.replace("\\", "/"))
        self.assertNotIn("__pycache__", result.output)

    def test_list_files_defaults_to_depth_two_and_hides_internal_dirs(self) -> None:
        result = list_files(".")

        self.assertEqual(result.status, "success")
        self.assertIn("pkg/", result.output.replace("\\", "/"))
        self.assertIn("pkg/target.py", result.output.replace("\\", "/"))
        self.assertNotIn(".harness", result.output)

    def test_read_file_blocks_observation_artifacts(self) -> None:
        result = read_file(".harness/observations/s/obs.txt")

        self.assertEqual(result.status, "failed")
        self.assertIn("[blocked]", result.output)


class ShellPolicyTests(unittest.TestCase):
    def test_bare_rg_is_rewritten_with_explicit_path_and_short_timeout(self) -> None:
        middleware = ToolPolicyMiddleware()
        args = {"command": 'rg -n "needle" --type py', "timeout": 300}

        blocked = middleware.before_tool("run_bash", args, [], runtime_state=AgentRuntimeState())

        self.assertIsNone(blocked)
        self.assertEqual(args["command"], 'rg -n "needle" --type py .')
        self.assertEqual(args["timeout"], 15)

    def test_recursive_shell_browse_repeated_once_triggers_fallback(self) -> None:
        middleware = ToolPolicyMiddleware()
        state = AgentRuntimeState()
        args = {"command": "Get-ChildItem -Recurse"}

        first = middleware.before_tool("run_bash", args, [], runtime_state=state)
        second = middleware.before_tool("run_bash", args, [], runtime_state=state)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertTrue(state.fallback.stop_requested)
        self.assertEqual(state.fallback.stop_reason, "repeated_tool_failure")

    def test_whole_repo_search_budget_blocks_excessive_root_searches(self) -> None:
        middleware = ToolPolicyMiddleware()
        state = AgentRuntimeState()
        args = {"pattern": "needle", "path": "."}

        for _ in range(4):
            self.assertIsNone(middleware.before_tool("repo_search", args, [], runtime_state=state))
        blocked = middleware.before_tool("repo_search", args, [], runtime_state=state)

        self.assertIsNotNone(blocked)
        self.assertIn("[blocked]", blocked.output)

    def test_successful_file_content_with_timeout_text_does_not_trigger_failure_fallback(self) -> None:
        middleware = ToolPolicyMiddleware(repeated_failure_threshold=2)
        state = AgentRuntimeState()
        args = {"path": "eval/benchmarks/run_terminal_bench.py", "max_lines": 40}
        result = "parser.add_argument('--tbench-timeout', type=int, default=7200)\n"

        first = middleware.post_tool("read_file", args, result, [], runtime_state=state)
        second = middleware.post_tool("read_file", args, result, [], runtime_state=state)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertFalse(state.fallback.stop_requested)


if __name__ == "__main__":
    unittest.main()
