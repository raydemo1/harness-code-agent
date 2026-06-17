"""Run the Terminal-Bench eval subset as its own heavy benchmark entrypoint."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT_BOOTSTRAP = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT_BOOTSTRAP) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_BOOTSTRAP))

from eval.scripts.eval_common import (
    PROJECT_ROOT,
    TBENCH_TASK_FILES,
    add_common_args,
    base_env,
    category_results,
    load_tbench_metadata,
    load_tbench_task_config,
    make_run_dir,
    number,
    safe_name,
    write_eval_reports,
    write_suite_summary,
)
from eval.benchmarks.usage_metrics import aggregate_usage


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    if args.dry_run:
        print(json.dumps(_dry_run_plan(args), ensure_ascii=False, indent=2))
        return 0

    run_tbench_suite(args)
    write_eval_reports(args.output_root)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Terminal-Bench eval tasks.")
    add_common_args(parser)
    parser.add_argument("--tbench-timeout", type=int, default=7200)
    parser.add_argument(
        "--tbench-task-set",
        choices=sorted(TBENCH_TASK_FILES),
        default="8task",
        help="Terminal-Bench task set size: 8task by default, or 24task for the larger subset.",
    )
    parser.add_argument(
        "--runner-env",
        default="",
        help="Optional Harbor environment backend, for example daytona.",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Ask Harbor to build task environments locally instead of pulling prebuilt images.",
    )
    return parser.parse_args(argv)


def run_tbench_suite(args: argparse.Namespace) -> Path:
    payload = load_tbench_task_config(args.tbench_task_set)
    metadata = load_tbench_metadata()
    tasks = list(payload["tasks"])
    run_dir = make_run_dir(args, "tbench")
    started = time.perf_counter()
    task_results: list[dict[str, Any]] = []
    outputs_dir = run_dir / "task_outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "eval" / "benchmarks" / "run_terminal_bench.py"),
            "--task",
            task,
        ]
        if args.runner_env:
            command.extend(["--env", args.runner_env])
        if args.force_build:
            command.append("--force-build")
        task_started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=base_env(),
            capture_output=True,
            text=True,
            timeout=args.tbench_timeout,
        )
        task_elapsed = time.perf_counter() - task_started
        stdout_path = outputs_dir / f"{safe_name(task)}.stdout.txt"
        stderr_path = outputs_dir / f"{safe_name(task)}.stderr.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8", newline="\n")
        stderr_path.write_text(completed.stderr, encoding="utf-8", newline="\n")
        task_meta = metadata.get(task) or {}
        launcher_result = _launcher_task_result(completed.stdout, task)
        launcher_passed = launcher_result.get("passed") if launcher_result else None
        status = _task_status(completed.returncode, launcher_passed)
        task_results.append({
            "task": task,
            "category": str(task_meta.get("category") or "unknown"),
            "difficulty": str(task_meta.get("difficulty") or "unknown"),
            "agent_timeout_sec": number(task_meta.get("agent_timeout_sec")),
            "returncode": completed.returncode,
            "status": status,
            "elapsed_seconds": task_elapsed,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "command": command,
            "reward": launcher_result.get("reward") if launcher_result else None,
            "trial_name": launcher_result.get("trial_name") if launcher_result else "",
            "session_id": launcher_result.get("session_id") if launcher_result else "",
            "metrics": (launcher_result.get("metrics") or {}) if launcher_result else {},
        })
    elapsed = time.perf_counter() - started
    passed = sum(1 for item in task_results if item["status"] == "passed")
    summary = {
        "suite": "tbench",
        "benchmark_name": str(payload.get("benchmark_name") or "Terminal-Bench 2.0 subset"),
        "task_set": str(payload.get("task_set") or args.tbench_task_set),
        "status": "passed" if passed == len(tasks) else "failed",
        "task_count": len(tasks),
        "passed": passed,
        "pass_rate": passed / len(tasks) if tasks else 0.0,
        "elapsed_seconds": elapsed,
        "tasks": tasks,
        "task_results": task_results,
        "category_results": category_results(task_results),
        **aggregate_usage(task_results),
    }
    write_suite_summary(run_dir, summary, [])
    return run_dir


def _dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_tbench_task_config(args.tbench_task_set)
    return {
        "runner": "terminal_bench",
        "output_root": str(Path(args.output_root)),
        "terminal_bench_task_set": args.tbench_task_set,
        "terminal_bench_benchmark_name": payload.get("benchmark_name"),
        "terminal_bench_tasks": payload["tasks"],
        "runner_env": args.runner_env,
        "force_build": args.force_build,
    }


def _launcher_task_result(stdout: str, task: str) -> dict[str, Any]:
    prefix = "HCA_TERMINAL_BENCH_RESULT:"
    for line in reversed((stdout or "").splitlines()):
        if prefix not in line:
            continue
        try:
            payload = json.loads(line.split(prefix, 1)[1].strip())
        except json.JSONDecodeError:
            continue
        for item in payload.get("task_results") or []:
            if item.get("task") == task:
                return item if isinstance(item, dict) else {}
    return {}


def _task_status(returncode: int, launcher_passed: Any) -> str:
    if launcher_passed is True:
        return "passed"
    if launcher_passed is False:
        return "failed"
    return "passed" if returncode == 0 else "failed"


def run_self_test() -> None:
    temp_dir = tempfile.mkdtemp()
    try:
        assert len(load_tbench_task_config("8task")["tasks"]) == 8
        assert len(load_tbench_task_config("24task")["tasks"]) == 24
        root = Path(temp_dir) / "results"
        run_dir = root / "tbench"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"suite": "tbench", "task_count": 1, "passed": 1, "pass_rate": 1.0}),
            encoding="utf-8",
        )
        write_eval_reports(root, report_output_dir=Path(temp_dir) / "out")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
