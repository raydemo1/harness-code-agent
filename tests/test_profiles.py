import os
import unittest
from unittest.mock import patch

from harness_code_agent.agent.prompts import (
    SHARED_AGENT_IDENTITY,
    GlobalRulesDoc,
    PromptPrefixBuilder,
)
from harness_code_agent.profiles import (
    PRODUCT_PROFILES,
    PROFILES,
    get_profile,
    list_profiles,
)
from harness_code_agent.profiles.router import (
    ROUTING_MODE_PINNED,
    route_profile_for_turn,
)
from harness_code_agent.profiles.terminal import TerminalProfile
from harness_code_agent.runtime.builtins.registry import BUILTIN_TOOL_REGISTRY
from harness_code_agent.runtime.middleware import (
    AcceptanceReviewMiddleware,
    PreExitVerificationMiddleware,
    RecoveryStrategyMiddleware,
    TaskTrackingEnforcementMiddleware,
    TerminalShellEditPolicyMiddleware,
)
from harness_code_agent.runtime.tool_registry import tool_schemas_for_profile


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

    def test_profile_router_prefers_explicit_workflow_contracts_over_similarity(self):
        cases = [
            ("先给我方案，不要修改代码", "plan"),
            ("只审查这个实现，不要改动文件", "review"),
            ("审查后直接修复这个 parser bug", "coding-agent"),
            ("创建一个响应式网页看板", "app-builder"),
            ("解释这段代码是什么意思", "general"),
        ]
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                decision = route_profile_for_turn(prompt, current_profile="general")
                self.assertEqual(decision.profile_name, expected)
                self.assertEqual(decision.reason, f"Explicit instruction contract matched {expected}.")
                self.assertEqual(decision.confidence, 1.0)

    def test_profile_router_keeps_specialized_profile_sticky_for_general_followup(self):
        decision = route_profile_for_turn("help me understand this concept", current_profile="coding-agent")

        self.assertEqual(decision.profile_name, "coding-agent")
        self.assertEqual(decision.action, "direct_answer")
        self.assertEqual(decision.matched_profile, "general")

    def test_pinned_profile_never_changes_from_semantic_similarity(self):
        decision = route_profile_for_turn(
            "先给我一个完整实施方案，不要修改代码",
            current_profile="coding-agent",
            routing_mode=ROUTING_MODE_PINNED,
        )

        self.assertEqual(decision.profile_name, "coding-agent")
        self.assertEqual(decision.action, "stay")
        self.assertEqual(decision.source, "pinned")
        self.assertEqual(decision.decisive_signal, "pinned")

    def test_semantic_routing_does_not_jump_between_specialized_profiles(self):
        decision = route_profile_for_turn(
            "refactor parser",
            current_profile="plan",
        )

        self.assertEqual(decision.profile_name, "plan")
        self.assertTrue(decision.fallback_used)

    def test_explicit_mode_can_transition_and_pins_at_session_layer(self):
        decision = route_profile_for_turn(
            "切换到编码模式",
            current_profile="plan",
        )

        self.assertEqual(decision.profile_name, "coding-agent")
        self.assertEqual(decision.decisive_signal, "explicit_mode")
        self.assertEqual(decision.action, "switch_profile")

    def test_low_evidence_route_keeps_non_unit_confidence(self):
        decision = route_profile_for_turn("嗯", current_profile="general")

        self.assertLess(decision.confidence, 1.0)
        self.assertTrue(decision.fallback_used)

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

    def test_general_profile_allows_parallel_commands_but_not_delegate_agents(self):
        cfg = get_profile("general").main_agent()
        tool_names = {
            schema["function"]["name"]
            for schema in tool_schemas_for_profile(
                allowed_permissions=cfg.allowed_tool_permissions,
                include_names=cfg.allowed_tool_names,
                exclude_names=cfg.blocked_tool_names,
                registry=BUILTIN_TOOL_REGISTRY,
            )
        }

        self.assertIn("parallel_commands", tool_names)
        self.assertNotIn("delegate_agent", tool_names)
        self.assertNotIn("parallel_agents", tool_names)

    def test_execution_profiles_share_acceptance_enforcement(self):
        for name in ("coding-agent", "app-builder"):
            with self.subTest(profile=name):
                cfg = get_profile(name).main_agent()
                tracking = [
                    mw for mw in cfg.middlewares
                    if isinstance(mw, TaskTrackingEnforcementMiddleware)
                ]

                self.assertEqual(cfg.initial_planning_mode, "unset")
                self.assertTrue(any(isinstance(mw, AcceptanceReviewMiddleware) for mw in cfg.middlewares))
                self.assertEqual(len(tracking), 1)
                self.assertTrue(tracking[0].enforce_acceptance)
                self.assertIsNone(tracking[0].require_start_after_n_actions)

                prompt = cfg.system_prompt
                self.assertIn("does not by itself make a task complex", prompt)
                self.assertNotIn("at most 2 low-risk actions", prompt)
                self.assertNotIn("up to 3 files", prompt)

    def test_read_only_and_planning_profiles_do_not_get_execution_acceptance_loop(self):
        for name in ("general", "plan", "review"):
            with self.subTest(profile=name):
                middlewares = get_profile(name).main_agent().middlewares

                self.assertFalse(any(isinstance(mw, AcceptanceReviewMiddleware) for mw in middlewares))
                self.assertFalse(
                    any(
                        isinstance(mw, TaskTrackingEnforcementMiddleware)
                        and mw.enforce_acceptance
                        for mw in middlewares
                    )
                )

    def test_terminal_keeps_eval_specific_shell_policy_without_pre_exit_verifier(self):
        cfg = get_profile("terminal").main_agent()

        self.assertEqual(cfg.initial_planning_mode, "tracked")
        self.assertTrue(any(isinstance(mw, AcceptanceReviewMiddleware) for mw in cfg.middlewares))
        self.assertTrue(any(isinstance(mw, TerminalShellEditPolicyMiddleware) for mw in cfg.middlewares))
        self.assertTrue(any(isinstance(mw, TaskTrackingEnforcementMiddleware) for mw in cfg.middlewares))
        self.assertTrue(any(isinstance(mw, RecoveryStrategyMiddleware) for mw in cfg.middlewares))
        self.assertFalse(any(isinstance(mw, PreExitVerificationMiddleware) for mw in cfg.middlewares))

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
