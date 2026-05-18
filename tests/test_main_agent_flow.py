import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

import config
from harness import Harness
import harness
from session import SessionStore
from profiles.base import AgentConfig


class FakeProfile:
    def __init__(self):
        self.legacy_calls = []

    def name(self):
        return "fake"

    def main_agent(self):
        return AgentConfig(
            system_prompt=(
                "Main agent owns all code changes, final integration, and stop decisions. "
                "Consult subagents only for local investigation, test design, parallel search, or review."
            ),
        )

    def subagent_policy(self):
        return {"allowed_scopes": ["codebase_investigation", "parallel_search", "test_design", "review"]}

    def acceptance_criteria(self):
        return ["main agent verifies the task before stopping"]

    def resolve_task_timeout(self, user_prompt):
        return None

    def planner(self):
        self.legacy_calls.append("planner")
        return AgentConfig(system_prompt="legacy planner")

    def builder(self):
        self.legacy_calls.append("builder")
        return AgentConfig(system_prompt="legacy builder")

    def evaluator(self):
        self.legacy_calls.append("evaluator")
        return AgentConfig(system_prompt="legacy evaluator")


class RecordingAgent:
    instances = []
    runs = []

    def __init__(self, name, system_prompt, use_tools=True, extra_tool_schemas=None,
                 middlewares=None, time_budget=None, **kwargs):
        self.name = name
        self.system_prompt = system_prompt
        self.use_tools = use_tools
        self.extra_tool_schemas = extra_tool_schemas or []
        self.middlewares = middlewares or []
        self.time_budget = time_budget
        self.tool_context = kwargs.get("tool_context")
        self.__class__.instances.append(self)

    def run(self, task):
        self.__class__.runs.append((self.name, task))
        return "done"


class WritingAgent(RecordingAgent):
    def run(self, task):
        super().run(task)
        self.tool_context.workspace.write_text("src/app.py", "print('hello')\n")
        self.tool_context.workspace.write_text(config.PROGRESS_FILE, "runtime progress\n")
        return "done"


class HarnessMainAgentFlowTests(unittest.TestCase):
    def setUp(self):
        self.old_workspace = config.WORKSPACE
        self.temp_dir = tempfile.mkdtemp()
        config.WORKSPACE = self.temp_dir
        os.environ["HARNESS_FLAT_WORKSPACE"] = "1"
        os.environ.pop("HARNESS_COMMIT_POLICY", None)
        RecordingAgent.instances = []
        RecordingAgent.runs = []

    def tearDown(self):
        config.WORKSPACE = self.old_workspace
        os.environ.pop("HARNESS_FLAT_WORKSPACE", None)
        os.environ.pop("HARNESS_COMMIT_POLICY", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _git(self, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=self.temp_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def test_harness_runs_only_the_main_agent(self):
        profile = FakeProfile()

        with patch("harness.Agent", RecordingAgent):
            Harness(profile).run("fix the issue")

        self.assertEqual(profile.legacy_calls, [])
        self.assertEqual([agent.name for agent in RecordingAgent.instances], ["main_agent"])
        self.assertEqual(len(RecordingAgent.runs), 1)
        self.assertEqual(RecordingAgent.runs[0][0], "main_agent")
        self.assertIn("fix the issue", RecordingAgent.runs[0][1])

    def test_harness_injects_acceptance_criteria_into_main_agent_task(self):
        profile = FakeProfile()

        with patch("harness.Agent", RecordingAgent):
            Harness(profile).run("fix the issue")

        task = RecordingAgent.runs[0][1]
        self.assertIn("Acceptance criteria", task)
        self.assertIn("main agent verifies the task before stopping", task)
        self.assertIn("Only the main agent may modify files", task)
        self.assertIn("Consultation sub-agents are read-only", task)

    def test_main_agent_prompt_owns_edits_integration_and_stop_decision(self):
        profile = FakeProfile()

        with patch("harness.Agent", RecordingAgent):
            Harness(profile)

        prompt = RecordingAgent.instances[0].system_prompt.lower()
        self.assertIn("main agent owns all code changes", prompt)
        self.assertIn("final integration", prompt)
        self.assertIn("stop decisions", prompt)
        self.assertIn("consult subagents", prompt)

    def test_harness_injects_prd_skill_routing_policy(self):
        profile = FakeProfile()

        with patch("harness.Agent", RecordingAgent):
            Harness(profile)

        prompt = RecordingAgent.instances[0].system_prompt
        self.assertIn("Skill routing policy", prompt)
        self.assertIn("skills/prd/SKILL.md", prompt)
        self.assertIn("skills/planning-with-files/SKILL.md", prompt)
        self.assertIn("skills/vibe-execution-guard/SKILL.md", prompt)
        self.assertIn("PRD.md", prompt)
        self.assertIn("task_plan.md", prompt)
        self.assertIn("findings.md", prompt)
        self.assertIn("progress.md", prompt)
        self.assertIn("planning-with-files", prompt)
        self.assertIn("Treat `prd` and `planning-with-files` as collaborators", prompt)

    def test_checkpoint_policy_commits_workspace_changes_after_session(self):
        os.environ["HARNESS_COMMIT_POLICY"] = "checkpoint"
        profile = FakeProfile()

        with patch("harness.Agent", WritingAgent):
            Harness(profile).run("save generated app")

        subjects = self._git("log", "--format=%s").splitlines()
        tracked_files = self._git("ls-files").splitlines()

        self.assertTrue(subjects[0].startswith("checkpoint: "))
        self.assertIn("src/app.py", tracked_files)
        self.assertNotIn(".harness", "\n".join(tracked_files))
        self.assertNotIn(config.PROGRESS_FILE, tracked_files)
        self.assertEqual("", self._git("status", "--porcelain", "--", "src/app.py"))

    def test_commit_policy_none_leaves_session_changes_uncommitted(self):
        os.environ["HARNESS_COMMIT_POLICY"] = "none"
        profile = FakeProfile()

        with patch("harness.Agent", WritingAgent):
            Harness(profile).run("save generated app")

        subjects = self._git("log", "--format=%s").splitlines()

        self.assertEqual(["init"], subjects)
        self.assertIn("?? src/app.py", self._git("status", "--porcelain", "--", "src/app.py"))

    def test_milestone_policy_commits_successful_session_with_milestone_message(self):
        os.environ["HARNESS_COMMIT_POLICY"] = "milestone"
        profile = FakeProfile()

        with patch("harness.Agent", WritingAgent):
            Harness(profile).run("save generated app")

        subjects = self._git("log", "--format=%s").splitlines()

        self.assertTrue(subjects[0].startswith("milestone: "))

    def test_sessions_command_lists_sessions_without_api_key(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="terminal",
            cwd=Path(self.temp_dir),
            model="model-a",
            permission_mode="workspace-write",
        )

        with patch.object(sys, "argv", ["harness.py", "sessions"]), patch("sys.stdout", new_callable=StringIO) as out:
            with self.assertRaises(SystemExit) as raised:
                import harness
                harness.main()

        self.assertEqual(raised.exception.code, 0)
        output = out.getvalue()
        self.assertIn(session.id, output)
        self.assertIn("terminal", output)

    def test_session_command_shows_session_without_api_key(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        session = store.create(
            profile="reasoning",
            cwd=Path(self.temp_dir),
            model="model-b",
            permission_mode="read-only",
        )
        store.event_bus(session).emit("session_started", agent="main_agent", payload={})

        with patch.object(sys, "argv", ["harness.py", "session", session.id]), patch("sys.stdout", new_callable=StringIO) as out:
            with self.assertRaises(SystemExit) as raised:
                import harness
                harness.main()

        self.assertEqual(raised.exception.code, 0)
        output = out.getvalue()
        self.assertIn(session.id, output)
        self.assertIn("reasoning", output)
        self.assertIn("events: 1", output)

    def test_fork_command_creates_session_fork_without_api_key(self):
        store = SessionStore(Path(self.temp_dir) / ".harness")
        source = store.create(
            profile="coding-agent",
            cwd=Path(self.temp_dir),
            model="model-c",
            permission_mode="workspace-write",
        )
        store.event_bus(source).emit("session_started", agent="main_agent", payload={})

        with patch.object(sys, "argv", ["harness.py", "fork", source.id]), patch("sys.stdout", new_callable=StringIO) as out:
            with self.assertRaises(SystemExit) as raised:
                harness.main()

        self.assertEqual(raised.exception.code, 0)
        output = out.getvalue()
        self.assertIn("forked_session:", output)
        self.assertIn(f"forked_from: {source.id}", output)

        forks = [
            item for item in store.list_sessions()
            if item.get("forked_from") == source.id
        ]
        self.assertEqual(len(forks), 1)
        self.assertEqual(forks[0]["profile"], "coding-agent")

    def test_config_show_prints_runtime_configuration_without_api_key(self):
        with (
            patch.object(config, "API_KEY", ""),
            patch.object(sys, "argv", ["harness.py", "config", "show"]),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            with self.assertRaises(SystemExit) as raised:
                import harness
                harness.main()

        self.assertEqual(raised.exception.code, 0)
        output = out.getvalue()
        self.assertIn("api_key: unset", output)
        self.assertIn(f"workspace: {self.temp_dir}", output)
        self.assertIn("commit_policy:", output)
        self.assertIn("permission_mode:", output)

    def test_doctor_reports_environment_without_api_key_preflight(self):
        with (
            patch.object(config, "API_KEY", ""),
            patch.object(sys, "argv", ["harness.py", "doctor"]),
            patch("sys.stdout", new_callable=StringIO) as out,
        ):
            with self.assertRaises(SystemExit) as raised:
                import harness
                harness.main()

        self.assertEqual(raised.exception.code, 1)
        output = out.getvalue()
        self.assertIn("Harness doctor", output)
        self.assertIn("API key", output)
        self.assertIn("Python", output)
        self.assertIn("Workspace", output)

    def test_run_command_defaults_to_coding_agent_profile(self):
        profile_name, task_args = harness._parse_profile_and_task(["run", "fix", "tests"])

        self.assertEqual(profile_name, "coding-agent")
        self.assertEqual(task_args, ["fix", "tests"])

    def test_legacy_task_invocation_keeps_app_builder_default(self):
        profile_name, task_args = harness._parse_profile_and_task(["build", "app"])

        self.assertEqual(profile_name, "app-builder")
        self.assertEqual(task_args, ["build", "app"])

    def test_run_command_allows_profile_override(self):
        profile_name, task_args = harness._parse_profile_and_task(
            ["run", "--profile", "terminal", "fix", "shell"]
        )

        self.assertEqual(profile_name, "terminal")
        self.assertEqual(task_args, ["fix", "shell"])


if __name__ == "__main__":
    unittest.main()
