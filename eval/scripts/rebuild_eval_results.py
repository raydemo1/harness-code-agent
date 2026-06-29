"""CLI for rebuilding the Terminal-Bench eval ledger and reports."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.scripts.eval_ledger import apply_retention_plan, rebuild_eval_ledger, write_outputs


DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
DEFAULT_JOBS_ROOT = PROJECT_ROOT / "jobs"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    ledger = rebuild_eval_ledger(
        results_root=Path(args.results_root),
        jobs_root=Path(args.jobs_root) if args.jobs_root else None,
        include_jobs=not args.no_include_jobs,
    )
    if args.apply_retention:
        deleted = apply_retention_plan(ledger["retention_plan"])
        print(f"Applied retention plan; deleted {len(deleted)} paths.")
    write_outputs(ledger, output_root=Path(args.results_root))
    summary = ledger["summary"]
    print(
        "Wrote eval ledger: "
        f"{summary['passed']}/{summary['total_tasks']} passed "
        f"({summary['pass_rate'] * 100:.1f}%), "
        f"{summary['fallback_2_0_tasks']} fallback from 2.0."
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild task-level eval ledger and reports.")
    parser.add_argument("--results-root", default=str(DEFAULT_RESULTS_ROOT))
    parser.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    parser.add_argument(
        "--no-include-jobs",
        action="store_true",
        help="Do not scan the top-level jobs root for legacy Harbor job summaries.",
    )
    parser.add_argument(
        "--apply-retention",
        action="store_true",
        help="Delete redundant artifacts from the generated retention plan. Default is dry-run.",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def run_self_test() -> None:
    temp_dir = Path(tempfile.mkdtemp())
    try:
        results = temp_dir / "results"
        trial = results / "2026-01-01_tbench_unit" / "harbor_jobs" / "example-task" / "2026-01-01__00-00-00" / "example-task__trial"
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "example-task",
                    "trial_name": "example-task__trial",
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "metrics": {"tokens": {"prompt_tokens": 10}, "tool_calls": {"total": 1}},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        ledger = rebuild_eval_ledger(results_root=results, jobs_root=temp_dir / "jobs")
        assert ledger["summary"]["total_tasks"] == 1
        assert ledger["summary"]["passed"] == 1
        write_outputs(ledger, output_root=results)
        assert (results / "SUMMARY.md").exists()
        assert (results / "ledger.json").exists()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
