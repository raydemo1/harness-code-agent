"""Run Claw-SWE-Bench as its own heavy benchmark entrypoint."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT_BOOTSTRAP = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT_BOOTSTRAP) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_BOOTSTRAP))

from eval.scripts.eval_common import (
    CLAW_TASK_FILES,
    PROJECT_ROOT,
    TASKS_ROOT,
    add_common_args,
    base_env,
    load_claw_task_config,
    run_suffix,
    write_eval_reports,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    if args.dry_run:
        print(json.dumps(_dry_run_plan(args), ensure_ascii=False, indent=2))
        return 0

    run_claw_suite(args)
    write_eval_reports(args.output_root)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Claw-SWE-Bench eval tasks.")
    add_common_args(parser)
    parser.add_argument(
        "--claw-task-set",
        choices=sorted(CLAW_TASK_FILES),
        default="lite80",
        help="Claw-SWE-Bench task set to run.",
    )
    parser.add_argument(
        "--claw-limit",
        type=int,
        default=0,
        help="Optional first-N Claw-SWE-Bench smoke limit before running the full task set.",
    )
    parser.add_argument("--claw-timeout", type=int, default=3600)
    parser.add_argument("--claw-workers", type=int, default=1)
    parser.add_argument("--claw-no-install-deps", action="store_true")
    return parser.parse_args(argv)


def run_claw_suite(args: argparse.Namespace) -> Path:
    task_config = load_claw_task_config(args.claw_task_set)
    command = [
        sys.executable,
        str(PROJECT_ROOT / "eval" / "benchmarks" / "run_claw_swe_bench.py"),
        "--task-config",
        str(TASKS_ROOT / CLAW_TASK_FILES[args.claw_task_set]),
        "--dataset-name",
        str(task_config["dataset_name"]),
        "--dataset-config",
        str(task_config["dataset_config"]),
        "--split",
        str(task_config.get("split") or "test"),
        "--timeout",
        str(args.claw_timeout),
        "--workers",
        str(args.claw_workers),
        "--output-root",
        str(Path(args.output_root)),
        "--run-name",
        run_suffix(args, "claw"),
    ]
    if args.claw_limit > 0:
        command.extend(["--limit", str(args.claw_limit)])
    if args.claw_no_install_deps:
        command.append("--no-install-deps")
    subprocess.run(command, cwd=PROJECT_ROOT, env=base_env(), check=True)
    return Path(args.output_root)


def _dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_claw_task_config(args.claw_task_set)
    return {
        "runner": "claw_swe_bench",
        "output_root": str(Path(args.output_root)),
        "claw_swe_bench_task_set": args.claw_task_set,
        "claw_swe_bench_benchmark_name": payload.get("benchmark_name"),
        "claw_swe_bench_dataset": {
            "dataset_name": payload.get("dataset_name"),
            "dataset_config": payload.get("dataset_config"),
            "split": payload.get("split"),
            "limit": args.claw_limit,
        },
    }


def run_self_test() -> None:
    temp_dir = tempfile.mkdtemp()
    try:
        payload = load_claw_task_config("lite80")
        assert payload["dataset_name"] == "TokenRhythm/Claw-SWE-Bench"
        root = Path(temp_dir) / "results"
        run_dir = root / "claw"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"suite": "claw_swe_bench", "task_count": 1, "patch_collected": 1}),
            encoding="utf-8",
        )
        write_eval_reports(root, report_output_dir=Path(temp_dir) / "out")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
