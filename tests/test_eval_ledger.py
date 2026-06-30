import json
import shutil
import tempfile
import unittest
from pathlib import Path


class EvalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.results = self.root / "eval" / "results"
        self.jobs = self.root / "jobs"
        self.results.mkdir(parents=True)
        self.jobs.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_uses_2_1_when_same_task_exists_in_2_0_and_2_1(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        self._write_summary(
            "2026-06-27_100000_tbench_old",
            benchmark_name="Terminal-Bench 2.0 24-task subset",
            task_set="24task",
            task_results=[self._task("same-task", "passed", reward=1.0, tool_calls=3)],
        )
        self._write_summary(
            "2026-06-28_100000_tbench_new",
            benchmark_name="Terminal-Bench 2.1 24-task subset",
            task_set="24task",
            task_results=[self._task("same-task", "failed", reward=0.0, tool_calls=9)],
        )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        final = ledger["final_results"][0]
        self.assertEqual(final["task"], "same-task")
        self.assertEqual(final["source_version"], "2.1")
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["selection_reason"], "latest_failure_2.1")
        self.assertEqual(ledger["summary"]["passed"], 0)

    def test_falls_back_to_2_0_when_task_has_no_2_1_attempts(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        self._write_summary(
            "2026-06-27_100000_tbench_old",
            benchmark_name="Terminal-Bench 2.0 24-task subset",
            task_set="24task",
            task_results=[self._task("old-only", "passed", reward=1.0, tool_calls=2)],
        )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        final = ledger["final_results"][0]
        self.assertEqual(final["task"], "old-only")
        self.assertEqual(final["source_version"], "2.0")
        self.assertEqual(final["selection_reason"], "fallback_2.0_latest_success")
        self.assertEqual(ledger["summary"]["fallback_2_0_tasks"], 1)
        self.assertEqual(ledger["summary"]["passed"], 1)

    def test_raw_harbor_result_without_summary_is_scanned(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        trial_dir = (
            self.results
            / "2026-06-28_120000_tbench_raw"
            / "harbor_jobs"
            / "raw-task"
            / "2026-06-28__12-00-00"
            / "raw-task__abc"
        )
        trial_dir.mkdir(parents=True)
        metrics = {
            "tokens": {"total_tokens": 42, "prompt_tokens": 30, "completion_tokens": 12},
            "tools": {"tool_calls": 5},
            "usage_cost": {"estimated_cost_usd": 0.001},
        }
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/raw-task",
                    "trial_name": "raw-task__abc",
                    "agent_result": {"metadata": {"hca_eval_metrics": metrics}},
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "exception_info": None,
                }
            ),
            encoding="utf-8",
        )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        self.assertEqual(ledger["summary"]["total_tasks"], 1)
        final = ledger["final_results"][0]
        self.assertEqual(final["task"], "raw-task")
        self.assertEqual(final["source_version"], "2.1")
        self.assertEqual(final["status"], "passed")
        self.assertEqual(final["tool_calls"], 5)

    def test_migrated_and_jobs_duplicates_are_deduped_by_attempt_signature(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        self._write_summary(
            "2026-06-28_120000_tbench_summary",
            benchmark_name="Terminal-Bench 2.1",
            task_set="manual",
            task_results=[self._task("dupe-task", "passed", reward=1.0, tool_calls=1)],
        )
        trial_dir = (
            self.results
            / "2026-06-28_120000_tbench_summary"
            / "harbor_jobs"
            / "dupe-task"
            / "2026-06-28__12-00-00"
            / "dupe-task__abc"
        )
        trial_dir.mkdir(parents=True)
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/dupe-task",
                    "trial_name": "dupe-task__abc",
                    "agent_result": {"metadata": {"hca_eval_metrics": {"tools": {"tool_calls": 7}}}},
                    "verifier_result": {"rewards": {"reward": 1.0}},
                }
            ),
            encoding="utf-8",
        )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        self.assertEqual(len([a for a in ledger["attempts"] if a["task"] == "dupe-task"]), 1)
        self.assertEqual(ledger["final_results"][0]["tool_calls"], 7)
        self.assertEqual(ledger["final_results"][0]["source"], "harbor_result.json")

    def test_success_preferred_and_failed_retention_keeps_three_recent_failures(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        self._write_summary(
            "2026-06-28_100000_tbench",
            benchmark_name="Terminal-Bench 2.1",
            task_set="manual",
            task_results=[self._task("eventual-pass", "failed", reward=0.0)],
        )
        self._write_summary(
            "2026-06-28_110000_tbench",
            benchmark_name="Terminal-Bench 2.1",
            task_set="manual",
            task_results=[self._task("eventual-pass", "passed", reward=1.0)],
        )
        for index in range(4):
            self._write_summary(
                f"2026-06-28_12{index:02d}00_tbench",
                benchmark_name="Terminal-Bench 2.1",
                task_set="manual",
                task_results=[self._task("always-fails", "failed", reward=0.0)],
            )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        final_by_task = {item["task"]: item for item in ledger["final_results"]}
        self.assertEqual(final_by_task["eventual-pass"]["status"], "passed")
        kept_failed = [
            item for item in ledger["retention_plan"]["kept_attempts"]
            if item["task"] == "always-fails"
        ]
        self.assertEqual(len(kept_failed), 3)

    def test_vision_required_tasks_are_excluded_from_main_results(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        self._write_summary(
            "2026-06-28_100000_tbench",
            benchmark_name="Terminal-Bench 2.1",
            task_set="manual",
            task_results=[
                self._task("code-from-image", "failed", reward=0.0, tool_calls=9),
                self._task("regular-task", "passed", reward=1.0, tool_calls=2),
            ],
        )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        self.assertEqual([item["task"] for item in ledger["final_results"]], ["regular-task"])
        self.assertEqual(ledger["summary"]["total_tasks"], 1)
        self.assertEqual(ledger["summary"]["passed"], 1)
        self.assertEqual(ledger["summary"]["excluded_task_count"], 1)
        self.assertEqual(ledger["summary"]["attempt_count"], 1)
        self.assertEqual(ledger["summary"]["excluded_attempt_count"], 1)
        self.assertEqual(ledger["excluded_results"][0]["task"], "code-from-image")
        self.assertEqual(ledger["excluded_results"][0]["exclusion_reason"], "vision_required")
        self.assertEqual(len([item for item in ledger["attempts"] if item["task"] == "code-from-image"]), 1)

    def test_unreadable_json_records_warning_without_failing(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        bad_dir = self.results / "2026-06-28_100000_tbench_bad"
        bad_dir.mkdir()
        (bad_dir / "summary.json").write_text("{not-json", encoding="utf-8")

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        self.assertEqual(ledger["summary"]["total_tasks"], 0)
        self.assertEqual(len(ledger["warnings"]), 1)
        self.assertIn("skip unreadable summary", ledger["warnings"][0])

    def test_retention_plan_omits_missing_delete_paths(self):
        from eval.scripts.eval_ledger import rebuild_eval_ledger

        self._write_summary(
            "2026-06-28_100000_tbench",
            benchmark_name="Terminal-Bench 2.1",
            task_set="manual",
            task_results=[
                {
                    **self._task("cleanup-task", "failed", reward=0.0),
                    "stdout_path": str(self.results / "missing.stdout.txt"),
                }
            ],
        )
        self._write_summary(
            "2026-06-28_110000_tbench",
            benchmark_name="Terminal-Bench 2.1",
            task_set="manual",
            task_results=[self._task("cleanup-task", "passed", reward=1.0)],
        )

        ledger = rebuild_eval_ledger(results_root=self.results, jobs_root=self.jobs)

        self.assertEqual(ledger["retention_plan"]["delete_attempts"], [])
        self.assertEqual(ledger["retention_plan"]["delete_path_count"], 0)

    def _write_summary(self, run_name, *, benchmark_name, task_set, task_results):
        run_dir = self.results / run_name
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "suite": "tbench",
                    "benchmark_name": benchmark_name,
                    "task_set": task_set,
                    "task_results": task_results,
                }
            ),
            encoding="utf-8",
        )

    def _task(self, task, status, *, reward=None, tool_calls=0):
        return {
            "task": task,
            "status": status,
            "reward": reward,
            "metrics": {
                "tokens": {"total_tokens": 100},
                "tools": {"tool_calls": tool_calls},
                "usage_cost": {"estimated_cost_usd": 0.01},
            },
        }


if __name__ == "__main__":
    unittest.main()
