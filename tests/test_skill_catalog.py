import sys
import types
import unittest
from pathlib import Path


def _install_fake_openai_module() -> None:
    openai = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            pass

    openai.OpenAI = OpenAI
    sys.modules["openai"] = openai


_install_fake_openai_module()

from harness_code_agent.skills.registry import SkillRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SkillCatalogTests(unittest.TestCase):
    def test_catalog_points_execution_tracking_to_runtime_state(self):
        prompt = SkillRegistry().build_catalog_prompt()

        self.assertIn("Planning Mode Self-Check", prompt)
        self.assertIn("update_plan_state", prompt)
        self.assertIn("PRD.md", prompt)
        self.assertNotIn('**prd**: "', prompt)
        self.assertNotIn('**vibe-execution-guard**: "', prompt)

    def test_catalog_includes_core_workflow_skills(self):
        prompt = SkillRegistry().build_catalog_prompt()

        for skill_name in [
            "diagnose",
            "tdd",
            "grill-with-docs",
            "handoff",
            "zoom-out",
            "frontend-debugging",
        ]:
            with self.subTest(skill=skill_name):
                self.assertIn(f"**{skill_name}**", prompt)

    def test_skill_files_do_not_include_scraped_metadata(self):
        forbidden_markers = [
            "Skill score",
            "Health score",
            "skillsbench",
            "Trigger phrases",
            "Privacy",
            "First Seen",
        ]

        for skill_file in sorted((PROJECT_ROOT / "skills").rglob("SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            for marker in forbidden_markers:
                with self.subTest(skill=skill_file, marker=marker):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
