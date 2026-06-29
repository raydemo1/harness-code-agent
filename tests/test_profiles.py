import os
import unittest
from unittest.mock import patch

from harness_code_agent.agent.prompts import (
    SHARED_AGENT_IDENTITY,
    GlobalRulesDoc,
    PromptPrefixBuilder,
)
from harness_code_agent.profiles import PRODUCT_PROFILES, PROFILES, get_profile, list_profiles
from harness_code_agent.profiles.terminal import TerminalProfile
from harness_code_agent.profiles.router import route_profile_for_turn


class ProfilePromptTests(unittest.TestCase):
    def test_product_registry_hides_eval_only_terminal_profile(self):
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
        self.assertEqual(
            list(PRODUCT_PROFILES),
            [
                "general",
                "coding-agent",
                "app-builder",
                "plan",
                "review",
            ],
        )
        self.assertEqual([item["name"] for item in list_profiles()], list(PRODUCT_PROFILES))
        self.assertIsInstance(get_profile("terminal"), TerminalProfile)
        with self.assertRaisesRegex(ValueError, "Unknown profile: swe-bench"):
            get_profile("swe-bench")

    def test_terminal_profile_stays_outside_product_auto_routing(self):
        decision = route_profile_for_turn("please build a web app", current_profile="terminal")

        self.assertEqual(decision.profile_name, "terminal")
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.fallback_reason, "profile is sticky")

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

    def test_terminal_profile_resolves_timeout_from_task_name_env(self):
        with patch.dict(os.environ, {"HARNESS_TERMINAL_TASK_NAME": "terminal-bench/overfull-hbox"}):
            timeout = TerminalProfile().resolve_task_timeout("instruction text without task slug")

        self.assertEqual(timeout, 750.0)

    def test_terminal_profile_resolves_task_metadata_from_task_name_env(self):
        with patch.dict(os.environ, {"HARNESS_TERMINAL_TASK_NAME": "terminal-bench/configure-git-webserver"}):
            metadata = TerminalProfile().resolve_task_metadata("workspace is /app")

        self.assertEqual(metadata["task_name"], "configure-git-webserver")
        self.assertEqual(metadata["category"], "system-administration")
        self.assertEqual(metadata["agent_timeout_sec"], 900.0)


if __name__ == "__main__":
    unittest.main()
