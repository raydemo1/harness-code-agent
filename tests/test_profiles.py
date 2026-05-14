import sys
import types
import unittest


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from profiles import get_profile, list_profiles
from profiles.base import AgentConfig


class ProfileInterfaceTests(unittest.TestCase):
    def test_all_profiles_expose_main_agent_and_subagent_policy(self):
        for profile_meta in list_profiles():
            profile = get_profile(profile_meta["name"])

            main_agent = profile.main_agent()
            policy = profile.subagent_policy()

            self.assertIsInstance(main_agent, AgentConfig)
            self.assertIn("consult_subagent", main_agent.system_prompt)
            self.assertIn("allowed_scopes", policy)
            self.assertIn("review", policy["allowed_scopes"])

    def test_terminal_main_agent_prompt_keeps_single_owner_model(self):
        profile = get_profile("terminal")
        prompt = profile.main_agent().system_prompt.lower()

        self.assertIn("you are the main agent", prompt)
        self.assertIn("only you may modify files", prompt)
        self.assertIn("consult_subagent", prompt)
        self.assertNotIn("planner", prompt)
        self.assertNotIn("builder", prompt)
        self.assertNotIn("evaluator", prompt)


if __name__ == "__main__":
    unittest.main()
