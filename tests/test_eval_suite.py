import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class EvalSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_terminal_bench_8task_subset_is_fixed_and_lightweight(self):
        payload = json.loads(Path("eval/tasks/terminal_bench_8task.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["suite"], "terminal_bench_8task")
        self.assertEqual(payload["task_set"], "8task")
        self.assertEqual(payload["benchmark_name"], "Terminal-Bench 2.0 8-task subset")
        self.assertEqual(
            payload["tasks"],
            [
                "fix-git",
                "overfull-hbox",
                "build-cython-ext",
                "custom-memory-heap-crash",
                "git-leak-recovery",
                "log-summary-date-ranges",
                "large-scale-text-editing",
                "query-optimize",
            ],
        )

    def test_terminal_bench_24task_subset_is_fixed_and_bounded(self):
        payload = json.loads(Path("eval/tasks/terminal_bench_24task.json").read_text(encoding="utf-8"))
        metadata = json.loads(Path("eval/benchmarks/tb2_tasks.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["suite"], "terminal_bench_24task")
        self.assertEqual(payload["task_set"], "24task")
        self.assertEqual(payload["benchmark_name"], "Terminal-Bench 2.0 24-task subset")
        self.assertEqual(
            payload["tasks"],
            [
                "fix-git",
                "overfull-hbox",
                "build-cython-ext",
                "custom-memory-heap-crash",
                "git-leak-recovery",
                "log-summary-date-ranges",
                "large-scale-text-editing",
                "query-optimize",
                "cancel-async-tasks",
                "kv-store-grpc",
                "polyglot-c-py",
                "sqlite-db-truncate",
                "nginx-request-logging",
                "git-multibranch",
                "configure-git-webserver",
                "fix-code-vulnerability",
                "sanitize-git-repo",
                "openssl-selfsigned-cert",
                "multi-source-data-merger",
                "sparql-university",
                "db-wal-recovery",
                "extract-elf",
                "adaptive-rejection-sampler",
                "model-extraction-relu-logits",
            ],
        )
        self.assertEqual(len(payload["tasks"]), 24)
        self.assertEqual(len(set(payload["tasks"])), 24)
        for task in payload["tasks"]:
            self.assertIn(task, metadata)
            self.assertLessEqual(float(metadata[task]["agent_timeout_sec"]), 1800.0)

    def test_terminal_bench_eval_dry_run_defaults_to_8task_and_can_select_24task(self):
        from eval.scripts.run_terminal_bench_eval import _dry_run_plan, parse_args

        default_args = parse_args(["--dry-run"])
        default_plan = _dry_run_plan(default_args)
        self.assertEqual(default_plan["terminal_bench_task_set"], "8task")
        self.assertEqual(len(default_plan["terminal_bench_tasks"]), 8)

        larger_args = parse_args(["--dry-run", "--tbench-task-set", "24task"])
        larger_plan = _dry_run_plan(larger_args)
        self.assertEqual(larger_plan["terminal_bench_task_set"], "24task")
        self.assertEqual(len(larger_plan["terminal_bench_tasks"]), 24)

    def test_claw_swe_bench_lite80_config_is_fixed(self):
        payload = json.loads(Path("eval/tasks/claw_swe_bench_lite80.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["suite"], "claw_swe_bench_lite80")
        self.assertEqual(payload["task_set"], "lite80")
        self.assertEqual(payload["benchmark_name"], "Claw-SWE-Bench Lite80")
        self.assertEqual(payload["dataset_name"], "TokenRhythm/Claw-SWE-Bench")
        self.assertEqual(payload["dataset_config"], "lite")
        self.assertEqual(payload["split"], "test")

    def test_deepseek_v4_flash_cost_uses_official_cache_split(self):
        from eval.benchmarks.usage_metrics import estimate_usage_cost

        cost = estimate_usage_cost(
            {
                "prompt_tokens": 1000,
                "cached_tokens": 800,
                "cache_miss_tokens": 200,
                "completion_tokens": 500,
            },
            model="deepseek-v4-flash",
        )

        self.assertEqual(cost["pricing_source"], "https://api-docs.deepseek.com/quick_start/pricing")
        self.assertEqual(cost["pricing_per_1m_tokens"]["input_cache_hit"], 0.0028)
        self.assertEqual(cost["pricing_per_1m_tokens"]["input_cache_miss"], 0.14)
        self.assertEqual(cost["pricing_per_1m_tokens"]["output"], 0.28)
        self.assertAlmostEqual(cost["estimated_cost_usd"], (800 * 0.0028 + 200 * 0.14 + 500 * 0.28) / 1_000_000)

    def test_legacy_deepseek_model_names_do_not_use_v4_pricing_alias(self):
        from eval.benchmarks.usage_metrics import estimate_usage_cost

        cost = estimate_usage_cost({"prompt_tokens": 1000, "completion_tokens": 500}, model="deepseek-reasoner")

        self.assertIsNone(cost["estimated_cost_usd"])
        self.assertIsNone(cost["pricing_per_1m_tokens"])

    def test_claw_swe_bench_eval_dry_run_includes_lite80(self):
        from eval.scripts.run_claw_swe_bench_eval import _dry_run_plan, parse_args

        args = parse_args(["--dry-run", "--claw-limit", "3"])
        plan = _dry_run_plan(args)

        self.assertEqual(plan["claw_swe_bench_task_set"], "lite80")
        self.assertEqual(plan["claw_swe_bench_benchmark_name"], "Claw-SWE-Bench Lite80")
        self.assertEqual(plan["claw_swe_bench_dataset"]["dataset_name"], "TokenRhythm/Claw-SWE-Bench")
        self.assertEqual(plan["claw_swe_bench_dataset"]["limit"], 3)

    def test_run_tbench_suite_records_per_task_category_results(self):
        from eval.scripts.run_terminal_bench_eval import parse_args, run_tbench_suite

        args = parse_args([
            "--tbench-task-set",
            "8task",
            "--output-root",
            str(self.root / "results"),
            "--run-name",
            "unit",
        ])

        def fake_run(command, **kwargs):
            task = command[-1]
            returncode = 1 if task == "overfull-hbox" else 0
            stdout = f"stdout {task}"
            if task == "fix-git":
                stdout += "\nHCA_TERMINAL_BENCH_RESULT:" + json.dumps({
                    "task_results": [{
                        "task": "fix-git",
                        "metrics": {
                            "turns": {"started": 1, "finished": 1},
                            "tokens": {
                                "llm_calls": 2,
                                "prompt_tokens": 100,
                                "cached_tokens": 40,
                                "cache_miss_tokens": 60,
                                "completion_tokens": 30,
                                "total_tokens": 130,
                            },
                            "tools": {"tool_calls": 3},
                            "usage_cost": {"estimated_cost_usd": 0.00002},
                        },
                    }]
                })
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=f"stderr {task}")

        with patch("eval.scripts.run_terminal_bench_eval.subprocess.run", side_effect=fake_run) as run_mock:
            run_dir = run_tbench_suite(args)

        self.assertEqual(run_mock.call_count, 8)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["task_set"], "8task")
        self.assertEqual(summary["task_count"], 8)
        self.assertEqual(summary["passed"], 7)
        self.assertAlmostEqual(summary["pass_rate"], 7 / 8)
        self.assertEqual(summary["category_results"]["debugging"]["task_count"], 3)
        self.assertEqual(summary["category_results"]["debugging"]["passed"], 2)
        self.assertEqual(summary["turn_totals"]["finished"], 1)
        self.assertEqual(summary["token_totals"]["total_tokens"], 130)
        self.assertEqual(summary["tool_calls"], 3)
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.00002)

    def test_basic_metrics_memory_cases_run_with_full_access_permissions(self):
        from eval.scripts.run_basic_metrics_eval import parse_args, run_memory_suite

        args = parse_args([
            "--suites",
            "memory",
            "--memory-limit",
            "1",
            "--output-root",
            str(self.root / "results"),
            "--run-name",
            "unit",
        ])

        def fake_run(command, **kwargs):
            self.assertEqual(kwargs["env"]["HARNESS_PERMISSION_MODE"], "danger-full-access")
            return subprocess.CompletedProcess(command, 0, stdout="hca session: missing\nmarker", stderr="")

        with (
            patch("eval.scripts.run_basic_metrics_eval.subprocess.run", side_effect=fake_run),
            patch("eval.scripts.run_basic_metrics_eval._session_metrics", return_value={}),
        ):
            run_dir = run_memory_suite(args)

        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["task_count"], 1)

    def test_run_claw_suite_invokes_launcher(self):
        from eval.scripts.run_claw_swe_bench_eval import parse_args, run_claw_suite

        args = parse_args([
            "--claw-limit",
            "2",
            "--output-root",
            str(self.root / "results"),
            "--run-name",
            "unit",
            "--claw-no-install-deps",
        ])

        with patch("eval.scripts.run_claw_swe_bench_eval.subprocess.run") as run_mock:
            run_claw_suite(args)

        command = run_mock.call_args.args[0]
        self.assertIn("run_claw_swe_bench.py", command[1])
        self.assertIn("--limit", command)
        self.assertIn("2", command)
        self.assertIn("--no-install-deps", command)

    def test_summarize_eval_writes_resume_report_from_suite_summaries(self):
        from eval.scripts.summarize_eval import summarize_result_root, write_reports

        results = self.root / "results"
        cache_dir = results / "2026-06-08_cache"
        memory_dir = results / "2026-06-08_memory_ab"
        latency_dir = results / "2026-06-08_latency"
        tbench_dir = results / "2026-06-08_tbench"
        claw_dir = results / "2026-06-08_claw"
        for path in (cache_dir, memory_dir, latency_dir, tbench_dir, claw_dir):
            path.mkdir(parents=True)

        (cache_dir / "summary.json").write_text(json.dumps({
            "scenarios": {
                "stable_warmup": {
                    "first_hit_ratio": 0.0,
                    "last_hit_ratio": 0.99,
                    "avg_hit_ratio": 0.79,
                },
                "compaction_rewrite": {
                    "prefix_changes": [{"label": "after_rewrite_turn_1", "reasons": ["log_rewrite"]}],
                    "last_hit_ratio": 0.91,
                },
            }
        }), encoding="utf-8")
        (memory_dir / "summary.json").write_text(json.dumps({
            "suite": "memory_ab",
            "task_count": 5,
            "uplift": {
                "tool_calls_reduction_ratio": 0.25,
                "elapsed_seconds_reduction_ratio": 0.2,
                "total_tokens_reduction_ratio": 0.18,
            },
        }), encoding="utf-8")
        (latency_dir / "summary.json").write_text(json.dumps({
            "suite": "latency",
            "turn_duration_ms": {"p50": 1000, "p95": 2500, "p99": 3000},
            "llm_response_latency_ms": {"p50": 700, "p95": 1700, "p99": 2100},
            "llm_first_token_ms": {"p50": 120, "p95": 300, "p99": 360},
        }), encoding="utf-8")
        (tbench_dir / "summary.json").write_text(json.dumps({
            "suite": "tbench",
            "benchmark_name": "Terminal-Bench 2.0 24-task subset",
            "task_set": "24task",
            "task_count": 24,
            "passed": 18,
            "pass_rate": 0.75,
            "token_totals": {"total_tokens": 4567},
            "turn_totals": {"finished": 24},
            "tool_calls": 90,
            "estimated_cost_usd": 0.1234,
            "category_results": {
                "debugging": {"task_count": 5, "passed": 4, "pass_rate": 0.8},
                "software-engineering": {"task_count": 6, "passed": 5, "pass_rate": 5 / 6},
            },
        }), encoding="utf-8")
        (claw_dir / "summary.json").write_text(json.dumps({
            "suite": "claw_swe_bench",
            "benchmark_name": "Claw-SWE-Bench Lite80",
            "task_set": "lite80",
            "task_count": 80,
            "patch_collected": 64,
            "patch_collection_rate": 0.8,
            "patch_empty": 4,
            "model": "deepseek-v4-flash",
            "token_totals": {"total_tokens": 123456},
        }), encoding="utf-8")

        summary = summarize_result_root(results)
        out_dir = self.root / "out"
        write_reports(summary, output_dir=out_dir)

        resume = (out_dir / "report_resume.md").read_text(encoding="utf-8")
        self.assertIn("Terminal-Bench 2.0 24-task subset", resume)
        self.assertIn("18/24", resume)
        self.assertIn("75.0%", resume)
        self.assertIn("99.0%", resume)
        self.assertIn("Memory A/B", resume)
        self.assertIn("p95", resume)
        self.assertIn("debugging 4/5", resume)
        self.assertIn("tokens=4567", resume)
        self.assertIn("est. cost=$0.1234", resume)
        self.assertIn("Claw-SWE-Bench Lite80", resume)
        self.assertIn("64/80 patches", resume)

    def test_harness_claw_adapter_builds_container_args_and_command(self):
        from eval.benchmarks.harness_claw_adapter import HarnessCodeAgentAdapter, _agent_command

        adapter = HarnessCodeAgentAdapter(
            model="deepseek-v4-flash",
            timeout=120,
            max_turns=12,
            repo_root=Path.cwd(),
            install_deps=False,
        )
        args = adapter.container_run_args("example__repo-1")
        joined = " ".join(args)
        self.assertIn("/opt/harness-code-agent:ro", joined)
        self.assertIn("HARNESS_WORKSPACE=/testbed", joined)
        self.assertIn("HARNESS_MODEL=deepseek-v4-flash", joined)
        self.assertIn("HARNESS_MODEL_HARD=deepseek-v4-flash", joined)
        self.assertIn("HARNESS_MODEL_INTENSITY=normal", joined)
        self.assertIn("MAX_AGENT_ITERATIONS=12", joined)

        command = _agent_command(timeout=120, max_turns=12)
        self.assertIn("PROFILE_SWE_BENCH_TASK_BUDGET=120", command)
        self.assertIn("hca_claw_runner.py", command)

    def test_eval_scripts_self_test(self):
        for script in (
            "eval/scripts/run_basic_metrics_eval.py",
            "eval/scripts/run_terminal_bench_eval.py",
            "eval/scripts/run_claw_swe_bench_eval.py",
            "eval/scripts/summarize_eval.py",
        ):
            completed = subprocess.run(
                [sys.executable, script, "--self-test"],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)


if __name__ == "__main__":
    unittest.main()
