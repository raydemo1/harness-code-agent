import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _install_fake_harbor_modules() -> None:
    harbor = types.ModuleType("harbor")
    harbor_agents = types.ModuleType("harbor.agents")
    harbor_agents_installed = types.ModuleType("harbor.agents.installed")
    harbor_agents_installed_base = types.ModuleType("harbor.agents.installed.base")
    harbor_environments = types.ModuleType("harbor.environments")
    harbor_environments_base = types.ModuleType("harbor.environments.base")
    harbor_models = types.ModuleType("harbor.models")
    harbor_models_agent = types.ModuleType("harbor.models.agent")
    harbor_models_agent_context = types.ModuleType("harbor.models.agent.context")

    class BaseInstalledAgent:
        def __init__(self, *args, **kwargs):
            pass

    def with_prompt_template(fn):
        return fn

    class BaseEnvironment:
        pass

    class AgentContext:
        pass

    harbor_agents_installed_base.BaseInstalledAgent = BaseInstalledAgent
    harbor_agents_installed_base.with_prompt_template = with_prompt_template
    harbor_environments_base.BaseEnvironment = BaseEnvironment
    harbor_models_agent_context.AgentContext = AgentContext

    sys.modules["harbor"] = harbor
    sys.modules["harbor.agents"] = harbor_agents
    sys.modules["harbor.agents.installed"] = harbor_agents_installed
    sys.modules["harbor.agents.installed.base"] = harbor_agents_installed_base
    sys.modules["harbor.environments"] = harbor_environments
    sys.modules["harbor.environments.base"] = harbor_environments_base
    sys.modules["harbor.models"] = harbor_models
    sys.modules["harbor.models.agent"] = harbor_models_agent
    sys.modules["harbor.models.agent.context"] = harbor_models_agent_context


class HarnessAgentInstallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _install_fake_harbor_modules()
        sys.modules.pop("eval.benchmarks.harbor_agent", None)
        self.module = importlib.import_module("eval.benchmarks.harbor_agent")

    async def test_install_uploads_current_repo_snapshot(self):
        commands = []
        uploads = []

        class RecordingEnvironment:
            async def upload_dir(self, source_dir, target_dir):
                uploads.append((source_dir, target_dir))

        class RecordingAgent(self.module.HarnessAgent):
            async def exec_as_root(self, environment, command, **kwargs):
                commands.append(("root", command))

            async def exec_as_agent(self, environment, command, **kwargs):
                commands.append(("agent", command))

        agent = RecordingAgent()
        await agent.install(environment=RecordingEnvironment())

        all_commands = "\n".join(command for _, command in commands)
        old_harness_repo = (
            "https://github.com/" + "lazyFrogLOL" + "/" + "Harness" + "_Engineering.git"
        )
        old_multi_agent_repo = "https://github.com/" + "lyx" + "hnu" + "/" + "multi" + "-agent.git"
        old_multi_agent_tarball = old_multi_agent_repo.replace(
            ".git",
            "/archive/refs/heads/main.tar.gz",
        )
        self.assertNotIn(
            old_harness_repo,
            all_commands,
        )
        self.assertNotIn(
            old_multi_agent_repo,
            all_commands,
        )
        self.assertNotIn(
            old_multi_agent_tarball,
            all_commands,
        )
        self.assertTrue(uploads)
        self.assertEqual(uploads[-1][1], "/home/user/harness-agent")

    async def test_snapshot_keeps_package_workspace_module(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            (root / "workspace").mkdir()
            (root / "jobs").mkdir()
            package_workspace = root / "harness_code_agent" / "workspace"
            package_workspace.mkdir(parents=True)
            (package_workspace / "__init__.py").write_text("", encoding="utf-8")

            dest = Path(temp_dir) / "snapshot"
            self.module._copy_repo_snapshot(root, dest)

            self.assertFalse((dest / "workspace").exists())
            self.assertFalse((dest / "jobs").exists())
            self.assertTrue((dest / "harness_code_agent" / "workspace" / "__init__.py").exists())

    async def test_run_invokes_headless_terminal_runner_from_task_workspace(self):
        commands = []
        envs = []

        class ExecResult:
            stdout = ""

        class RecordingAgent(self.module.HarnessAgent):
            async def exec_as_agent(self, environment, command, env=None):
                commands.append(command)
                envs.append(env or {})
                return ExecResult()

        agent = RecordingAgent()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://api.example.test",
                "HARNESS_MODEL": "test-model",
                "HARNESS_MODEL_INTENSITY": "normal",
            },
        ):
            await agent.run("fix shell", environment=object(), context=object())

        command = commands[-1]
        self.assertIn("WORKSPACE=/app", command)
        self.assertIn('cd "$WORKSPACE" &&', command)
        self.assertIn("PYTHONPATH=/home/user/harness-agent", command)
        self.assertIn("/home/user/harness-agent/eval/benchmarks/hca_terminal_runner.py", command)
        self.assertIn('--workspace "$WORKSPACE"', command)
        self.assertNotIn("python3 -m harness_code_agent.cli", command)
        self.assertNotIn("OPENAI_API_KEY", command)
        self.assertEqual(envs[-1]["OPENAI_API_KEY"], "test-key")
        self.assertEqual(envs[-1]["HARNESS_MODEL_INTENSITY"], "normal")
        self.assertNotIn("HARNESS_WORKSPACE=/app", command)

    async def test_run_populates_agent_context_with_usage_metrics(self):
        class ExecResult:
            stdout = (
                "assistant done\n"
                'HCA_EVAL_METRICS:{"session_id":"s1","tokens":{"prompt_tokens":100,'
                '"cached_tokens":40,"completion_tokens":30},"usage_cost":{"estimated_cost_usd":0.02}}\n'
            )

        class Context:
            n_input_tokens = None
            n_cache_tokens = None
            n_output_tokens = None
            cost_usd = None
            metadata = None

        class RecordingAgent(self.module.HarnessAgent):
            async def exec_as_agent(self, environment, command, env=None):
                return ExecResult()

        context = Context()
        agent = RecordingAgent()
        await agent.run("fix shell", environment=object(), context=context)

        self.assertEqual(context.n_input_tokens, 100)
        self.assertEqual(context.n_cache_tokens, 40)
        self.assertEqual(context.n_output_tokens, 30)
        self.assertEqual(context.cost_usd, 0.02)
        self.assertEqual(context.metadata["hca_eval_metrics"]["session_id"], "s1")


if __name__ == "__main__":
    unittest.main()
