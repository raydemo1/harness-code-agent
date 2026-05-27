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

from harness_code_agent.skills.registry import SkillRegistry


class SkillCatalogTests(unittest.TestCase):
    def test_catalog_points_execution_tracking_to_runtime_state(self):
        prompt = SkillRegistry().build_catalog_prompt()

        self.assertIn("Planning Mode Self-Check", prompt)
        self.assertIn("update_plan_state", prompt)
        self.assertIn("PRD.md", prompt)


if __name__ == "__main__":
    unittest.main()
