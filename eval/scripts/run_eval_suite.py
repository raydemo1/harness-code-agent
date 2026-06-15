"""Run the lightweight interview-project agent evaluation suites."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness_code_agent.memory.dream import run_dream
from harness_code_agent.memory.store import MemoryStore
from harness_code_agent.sessions.observability import (
    build_session_observability,
)
from harness_code_agent.sessions.store import SessionStore

from eval.scripts.summarize_eval import summarize_result_root, write_reports


RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
TASKS_ROOT = PROJECT_ROOT / "eval" / "tasks"
TBENCH_METADATA_PATH = PROJECT_ROOT / "eval" / "benchmarks" / "tb2_tasks.json"
TBENCH_TASK_FILES = {
    "8task": "terminal_bench_8task.json",
    "24task": "terminal_bench_24task.json",
}


@dataclass
class CaseResult:
    suite: str
    case_id: str
    variant: str
    returncode: int
    elapsed_seconds: float
    success: bool
    session_id: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metrics"] = dict(self.metrics or {})
        return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0

    suites = _suite_names(args.suites)
    if args.dry_run:
        print(json.dumps(_dry_run_plan(suites, args), ensure_ascii=False, indent=2))
        return 0

    for suite in suites:
        if suite == "cache":
            run_cache_suite(args)
        elif suite == "memory":
            run_memory_suite(args)
        elif suite == "latency":
            run_latency_suite(args)
        elif suite == "tbench":
            run_tbench_suite(args)
        else:
            raise ValueError(f"Unknown suite: {suite}")

    summary = summarize_result_root(Path(args.output_root))
    write_reports(summary, output_dir=PROJECT_ROOT / "eval")
    print(f"Wrote resume report: {PROJECT_ROOT / 'eval' / 'report_resume.md'}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lightweight agent eval suites.")
    parser.add_argument(
        "--suites",
        default="cache,memory,latency,tbench",
        help="Comma-separated suites: cache,memory,latency,tbench",
    )
    parser.add_argument("--output-root", default=str(RESULTS_ROOT))
    parser.add_argument("--run-name", default="")
    parser.add_argument("--task-timeout", type=int, default=900)
    parser.add_argument("--tbench-timeout", type=int, default=7200)
    parser.add_argument(
        "--tbench-task-set",
        choices=sorted(TBENCH_TASK_FILES),
        default="8task",
        help="Terminal-Bench task set size to run: 8task by default, or 24task for the larger subset.",
    )
    parser.add_argument("--cache-turns", type=int, default=5)
    parser.add_argument("--latency-limit", type=int, default=10)
    parser.add_argument("--memory-limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def run_cache_suite(args: argparse.Namespace) -> Path:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "eval" / "scripts" / "deepseek_context_eval.py"),
        "--scenarios",
        "stable_warmup,schema_reorder,compaction_rewrite",
        "--turns",
        str(args.cache_turns),
        "--project-context-tokens",
        "12000",
        "--max-output-tokens",
        "800",
        "--summary-output-tokens",
        "1200",
        "--post-rewrite-turns",
        "12",
        "--output-root",
        str(Path(args.output_root)),
        "--run-name",
        _run_suffix(args, "cache"),
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    return Path(args.output_root)


def run_memory_suite(args: argparse.Namespace) -> Path:
    tasks = _load_task_list("memory_ab.json")["tasks"][: max(1, args.memory_limit)]
    run_dir = _make_run_dir(args, "memory_ab")
    raw_path = run_dir / "raw_cases.jsonl"
    results: list[CaseResult] = []
    for task in tasks:
        for variant in ("baseline", "treatment"):
            env = _base_env()
            memory_root = run_dir / "memory_roots" / str(task["id"]) / variant
            env["HARNESS_MEMORY_ROOT"] = str(memory_root)
            env["HARNESS_STREAM"] = "0"
            if variant == "baseline":
                env["HARNESS_MEMORY_DISABLED"] = "1"
            else:
                env.pop("HARNESS_MEMORY_DISABLED", None)
                _seed_memory(memory_root, task)
            result = _run_hca_case(
                suite="memory_ab",
                case_id=str(task["id"]),
                variant=variant,
                prompt=str(task["prompt"]),
                profile=str(task.get("profile") or "coding-agent"),
                env=env,
                run_dir=run_dir,
                timeout=args.task_timeout,
                success_markers=list(task.get("success_markers") or []),
            )
            results.append(result)
            _append_jsonl(raw_path, result.to_dict())

    summary = _memory_summary(results)
    _write_suite_summary(run_dir, summary, results)
    return run_dir


def run_latency_suite(args: argparse.Namespace) -> Path:
    tasks = _load_task_list("latency_smoke.json")["tasks"][: max(1, args.latency_limit)]
    run_dir = _make_run_dir(args, "latency")
    raw_path = run_dir / "raw_cases.jsonl"
    results: list[CaseResult] = []
    for task in tasks:
        env = _base_env()
        env["HARNESS_STREAM"] = "1"
        result = _run_hca_case(
            suite="latency",
            case_id=str(task["id"]),
            variant="streaming",
            prompt=str(task["prompt"]),
            profile=str(task.get("profile") or "coding-agent"),
            env=env,
            run_dir=run_dir,
            timeout=args.task_timeout,
            success_markers=[],
        )
        results.append(result)
        _append_jsonl(raw_path, result.to_dict())

    summary = _latency_summary(results)
    _write_suite_summary(run_dir, summary, results)
    return run_dir


def run_tbench_suite(args: argparse.Namespace) -> Path:
    payload = _load_tbench_task_config(args.tbench_task_set)
    metadata = _load_tbench_metadata()
    tasks = list(payload["tasks"])
    run_dir = _make_run_dir(args, "tbench")
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
        task_started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=_base_env(),
            capture_output=True,
            text=True,
            timeout=args.tbench_timeout,
        )
        task_elapsed = time.perf_counter() - task_started
        stdout_path = outputs_dir / f"{_safe_name(task)}.stdout.txt"
        stderr_path = outputs_dir / f"{_safe_name(task)}.stderr.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8", newline="\n")
        stderr_path.write_text(completed.stderr, encoding="utf-8", newline="\n")
        task_meta = metadata.get(task) or {}
        task_results.append({
            "task": task,
            "category": str(task_meta.get("category") or "unknown"),
            "difficulty": str(task_meta.get("difficulty") or "unknown"),
            "agent_timeout_sec": _number(task_meta.get("agent_timeout_sec")),
            "returncode": completed.returncode,
            "status": "passed" if completed.returncode == 0 else "failed",
            "elapsed_seconds": task_elapsed,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "command": command,
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
        "category_results": _category_results(task_results),
    }
    _write_suite_summary(run_dir, summary, [])
    return run_dir


def _run_hca_case(
    *,
    suite: str,
    case_id: str,
    variant: str,
    prompt: str,
    profile: str,
    env: dict[str, str],
    run_dir: Path,
    timeout: int,
    success_markers: list[str],
) -> CaseResult:
    case_dir = run_dir / "cases" / _safe_name(case_id) / _safe_name(variant)
    case_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "harness_code_agent.cli", "--profile", profile, "-p", prompt]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    stdout_path = case_dir / "stdout.txt"
    stderr_path = case_dir / "stderr.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8", newline="\n")
    stderr_path.write_text(completed.stderr, encoding="utf-8", newline="\n")
    session_id = _session_id_from_stdout(completed.stdout)
    metrics = _session_metrics(session_id) if session_id else {}
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    marker_success = all(str(marker).lower() in combined for marker in success_markers)
    success = completed.returncode == 0 and (marker_success if success_markers else True)
    return CaseResult(
        suite=suite,
        case_id=case_id,
        variant=variant,
        returncode=completed.returncode,
        elapsed_seconds=elapsed,
        success=success,
        session_id=session_id,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        metrics=metrics,
    )


def _seed_memory(memory_root: Path, task: dict[str, Any]) -> None:
    store = MemoryStore(memory_root, workspace=PROJECT_ROOT)
    for candidate in task.get("memory_records") or []:
        payload = dict(candidate)
        payload.setdefault("confidence", 0.95)
        payload.setdefault("source_sessions", ["eval_seed"])
        store.append_candidate(payload)
    run_dream(store)


def _session_metrics(session_id: str) -> dict[str, Any]:
    store = SessionStore(PROJECT_ROOT / ".harness")
    try:
        metadata = store.read_metadata(session_id)
        events = store.read_events(session_id)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {}
    snapshot = build_session_observability(metadata, events)
    return snapshot.to_dict()


def _memory_summary(results: list[CaseResult]) -> dict[str, Any]:
    baseline = [item for item in results if item.variant == "baseline"]
    treatment = [item for item in results if item.variant == "treatment"]
    base = _aggregate_case_results(baseline)
    treat = _aggregate_case_results(treatment)
    return {
        "suite": "memory_ab",
        "task_count": len({item.case_id for item in results}),
        "baseline": base,
        "treatment": treat,
        "uplift": {
            "tool_calls_reduction_ratio": _reduction(base["tool_calls"], treat["tool_calls"]),
            "elapsed_seconds_reduction_ratio": _reduction(base["elapsed_seconds"], treat["elapsed_seconds"]),
            "total_tokens_reduction_ratio": _reduction(base["total_tokens"], treat["total_tokens"]),
            "success_delta": treat["success_rate"] - base["success_rate"],
        },
    }


def _latency_summary(results: list[CaseResult]) -> dict[str, Any]:
    turn_values: list[int] = []
    response_values: list[int] = []
    first_token_values: list[int] = []
    for result in results:
        perf = ((result.metrics or {}).get("performance") or {})
        turn_values.extend(_metric_values(perf.get("turn_duration_ms"), fallback_ms=result.elapsed_seconds * 1000))
        response_values.extend(_metric_values(perf.get("llm_response_latency_ms")))
        first_token_values.extend(_metric_values(perf.get("llm_first_token_ms")))
    return {
        "suite": "latency",
        "task_count": len(results),
        "success_rate": _success_rate(results),
        "turn_duration_ms": _distribution(turn_values),
        "llm_response_latency_ms": _distribution(response_values),
        "llm_first_token_ms": _distribution(first_token_values),
    }


def _aggregate_case_results(results: list[CaseResult]) -> dict[str, Any]:
    return {
        "cases": len(results),
        "successes": sum(1 for item in results if item.success),
        "success_rate": _success_rate(results),
        "elapsed_seconds": sum(item.elapsed_seconds for item in results),
        "tool_calls": sum(_nested_int(item.metrics, "tools", "tool_calls") for item in results),
        "llm_calls": sum(_nested_int(item.metrics, "tokens", "llm_calls") for item in results),
        "prompt_tokens": sum(_nested_int(item.metrics, "tokens", "prompt_tokens") for item in results),
        "total_tokens": sum(_nested_int(item.metrics, "tokens", "total_tokens") for item in results),
    }


def _metric_values(payload: Any, *, fallback_ms: float | None = None) -> list[int]:
    values: list[int] = []
    if isinstance(payload, dict) and int(payload.get("count") or 0) > 0:
        for key in ("p50", "p95", "p99"):
            value = payload.get(key)
            if value is not None:
                values.append(int(round(float(value))))
    if not values and fallback_ms is not None:
        values.append(int(round(fallback_ms)))
    return values


def _distribution(values: list[int]) -> dict[str, Any]:
    values = sorted(value for value in values if value >= 0)
    return {
        "count": len(values),
        "min": values[0] if values else 0,
        "max": values[-1] if values else 0,
        "mean": sum(values) / len(values) if values else 0.0,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def _write_suite_summary(run_dir: Path, summary: dict[str, Any], results: list[CaseResult]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    lines = [f"# {summary.get('suite', 'eval')} Summary", ""]
    lines.append("```json")
    lines.append(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    lines.append("```")
    (run_dir / "report_internal.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if results:
        (run_dir / "cases.json").write_text(
            json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def _load_task_list(filename: str) -> dict[str, Any]:
    path = TASKS_ROOT / filename
    return json.loads(path.read_text(encoding="utf-8"))


def _load_tbench_task_config(task_set: str) -> dict[str, Any]:
    filename = TBENCH_TASK_FILES[task_set]
    return _load_task_list(filename)


def _load_tbench_metadata() -> dict[str, Any]:
    return json.loads(TBENCH_METADATA_PATH.read_text(encoding="utf-8"))


def _make_run_dir(args: argparse.Namespace, suite: str) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = f"_{_safe_name(args.run_name)}" if args.run_name else ""
    run_dir = Path(args.output_root) / f"{timestamp}_{suite}{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _run_suffix(args: argparse.Namespace, suite: str) -> str:
    return f"{suite}_{_safe_name(args.run_name)}" if args.run_name else suite


def _suite_names(value: str) -> list[str]:
    names = [item.strip().lower() for item in value.split(",") if item.strip()]
    return names or ["cache", "memory", "latency", "tbench"]


def _dry_run_plan(suites: list[str], args: argparse.Namespace) -> dict[str, Any]:
    tbench_payload = _load_tbench_task_config(args.tbench_task_set)
    return {
        "suites": suites,
        "output_root": str(Path(args.output_root)),
        "memory_tasks": len(_load_task_list("memory_ab.json")["tasks"]),
        "latency_tasks": len(_load_task_list("latency_smoke.json")["tasks"]),
        "terminal_bench_task_set": args.tbench_task_set,
        "terminal_bench_benchmark_name": tbench_payload.get("benchmark_name"),
        "terminal_bench_tasks": tbench_payload["tasks"],
    }


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) if not existing else str(PROJECT_ROOT) + os.pathsep + existing
    return env


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _session_id_from_stdout(stdout: str) -> str:
    match = re.search(r"^hca session:\s*(\S+)", stdout, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _nested_int(payload: dict[str, Any] | None, *keys: str) -> int:
    value: Any = payload or {}
    for key in keys:
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _success_rate(results: list[CaseResult]) -> float:
    if not results:
        return 0.0
    return sum(1 for item in results if item.success) / len(results)


def _reduction(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return (before - after) / before


def _category_results(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, int]] = {}
    for item in task_results:
        category = str(item.get("category") or "unknown")
        bucket = grouped.setdefault(category, {"task_count": 0, "passed": 0})
        bucket["task_count"] += 1
        if item.get("status") == "passed":
            bucket["passed"] += 1
    return {
        category: {
            "task_count": values["task_count"],
            "passed": values["passed"],
            "pass_rate": values["passed"] / values["task_count"] if values["task_count"] else 0.0,
        }
        for category, values in sorted(grouped.items())
    }


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    idx = max(0, min(len(values) - 1, int((len(values) * quantile + 0.999999) - 1)))
    return sorted(values)[idx]


def _safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value).strip())
    return text.strip("_") or "run"


def run_self_test() -> None:
    temp_dir = tempfile.mkdtemp()
    try:
        assert len(_load_task_list("terminal_bench_8task.json")["tasks"]) == 8
        assert len(_load_task_list("terminal_bench_24task.json")["tasks"]) == 24
        assert len(_load_task_list("memory_ab.json")["tasks"]) >= 5
        assert len(_load_task_list("latency_smoke.json")["tasks"]) >= 10
        root = Path(temp_dir) / "results"
        for suite in ("memory_ab", "latency", "tbench"):
            run_dir = root / suite
            run_dir.mkdir(parents=True)
            (run_dir / "summary.json").write_text(
                json.dumps({"suite": suite, "task_count": 1, "passed": 1, "pass_rate": 1.0}),
                encoding="utf-8",
            )
        summary = summarize_result_root(root)
        out = Path(temp_dir) / "out"
        write_reports(summary, output_dir=out)
        assert (out / "report_resume.md").exists()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
