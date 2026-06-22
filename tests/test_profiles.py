import unittest

from harness_code_agent.agent.prompts import (
    SHARED_AGENT_IDENTITY,
    GlobalRulesDoc,
    PromptPrefixBuilder,
)
from harness_code_agent.profiles import PROFILES, get_profile, list_profiles


class ProfilePromptTests(unittest.TestCase):
    def test_product_registry_contains_six_profiles_without_swe_bench(self):
        self.assertEqual(
            list(PROFILES),
            [
                "general",
                "coding-agent",
                "app-builder",
                "terminal",
                "plan",
                "review",
            ],
        )
        self.assertEqual([item["name"] for item in list_profiles()], list(PROFILES))
        with self.assertRaisesRegex(ValueError, "Unknown profile: swe-bench"):
            get_profile("swe-bench")

    def test_shared_identity_precedes_profile_contract_and_has_own_hash(self):
        prefix = PromptPrefixBuilder().build(
            profile_prompt="## Role\nA focused test profile.",
            global_rules_docs=[
                GlobalRulesDoc(source="HARNESS.md", content="Use focused checks.")
            ],
            acceptance_criteria=["The requested behavior is verified."],
        )

        self.assertIn("## Agent Identity and Judgment", prefix.content)
        self.assertIn(SHARED_AGENT_IDENTITY, prefix.content)
        self.assertIn("## Profile Contract", prefix.content)
        self.assertLess(
            prefix.content.index("## Agent Identity and Judgment"),
            prefix.content.index("## Profile Contract"),
        )
        self.assertLess(
            prefix.content.index("## Profile Contract"),
            prefix.content.index("## Global Rules Bundle"),
        )
        self.assertIn("shared_identity_hash", prefix.hashes)

    def test_each_profile_has_role_working_style_boundaries_and_completion(self):
        for name in PROFILES:
            with self.subTest(profile=name):
                prompt = get_profile(name).main_agent().system_prompt
                self.assertIn("## Role", prompt)
                self.assertIn("## Working Style", prompt)
                self.assertIn("## Boundaries", prompt)
                self.assertIn("## Completion", prompt)

    def test_profile_contracts_keep_their_distinctive_behavior(self):
        prompts = {
            name: get_profile(name).main_agent().system_prompt.lower()
            for name in PROFILES
        }

        self.assertIn("answer-first", prompts["general"])
        self.assertIn("existing design", prompts["coding-agent"])
        self.assertIn("decision-complete", prompts["plan"])
        self.assertIn("findings first", prompts["review"])
        self.assertIn("non-interactive", prompts["terminal"])
        self.assertIn("smallest suitable stack", prompts["app-builder"])


if __name__ == "__main__":
    unittest.main()
