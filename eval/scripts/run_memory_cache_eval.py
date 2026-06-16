"""Run local memory/cache evals without importing benchmark launchers."""
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
    CaseResult,
    add_common_args,
    append_jsonl,
    base_env,
    load_task_list,
    make_run_dir,
    nested_int,
    percentile,
    reduction,
    run_suffix,
    session_id_from_stdout,
    success_rate,
    suite_names,
    write_eval_reports,
    write_suite_summary,
)

from harness_code_agent.memory.dream import run_dream
from harness_code_agent.memory.store import MemoryStore
from harness_code_agent.sessions.observability import build_session_observability
from harness_code_agent.sessions.store import SessionStore


LOCAL_SUITES = {"cache", "memory", "latency"}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0

    suites = _local_suite_names(args.suites)
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
        else:
            raise ValueError(f"Unknown local suite: {suite}")

    write_eval_reports(args.output_root)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local cache, memory, and latency eval suites.")
    parser.add_argument(
        "--suites",
        default="cache,memory,latency",
        help="Comma-separated local suites: cache,memory,latency. Defaults to cache,memory,latency.",
    )
    add_common_args(parser)
    parser.add_argument("--task-timeout", type=int, default=900)
    parser.add_argument("--cache-turns", type=int, default=5)
    parser.add_argument("--latency-limit", type=int, default=10)
    parser.add_argument("--memory-limit", type=int, default=5)
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
        run_suffix(args, "cache"),
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    return Path(args.output_root)


def run_memory_suite(args: argparse.Namespace) -> Path:
    tasks = load_task_list("memory_ab.json")["tasks"][: max(1, args.memory_limit)]
    run_dir = make_run_dir(args, "memory_ab")
    raw_path = run_dir / "raw_cases.jsonl"
    results: list[CaseResult] = []
    for task in tasks:
        for variant in ("baseline", "treatment"):
            env = base_env()
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
            append_jsonl(raw_path, result.to_dict())

    summary = _memory_summary(results)
    write_suite_summary(run_dir, summary, results)
    return run_dir


def run_latency_suite(args: argparse.Namespace) -> Path:
    tasks = load_task_list("latency_smoke.json")["tasks"][: max(1, args.latency_limit)]
    run_dir = make_run_dir(args, "latency")
    raw_path = run_dir / "raw_cases.jsonl"
    results: list[CaseResult] = []
    for task in tasks:
        env = base_env()
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
        append_jsonl(raw_path, result.to_dict())

    summary = _latency_summary(results)
    write_suite_summary(run_dir, summary, results)
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
    session_id = session_id_from_stdout(completed.stdout)
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
            "tool_calls_reduction_ratio": reduction(base["tool_calls"], treat["tool_calls"]),
            "elapsed_seconds_reduction_ratio": reduction(base["elapsed_seconds"], treat["elapsed_seconds"]),
            "total_tokens_reduction_ratio": reduction(base["total_tokens"], treat["total_tokens"]),
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
        "success_rate": success_rate(results),
        "turn_duration_ms": _distribution(turn_values),
        "llm_response_latency_ms": _distribution(response_values),
        "llm_first_token_ms": _distribution(first_token_values),
    }


def _aggregate_case_results(results: list[CaseResult]) -> dict[str, Any]:
    return {
        "cases": len(results),
        "successes": sum(1 for item in results if item.success),
        "success_rate": success_rate(results),
        "elapsed_seconds": sum(item.elapsed_seconds for item in results),
        "tool_calls": sum(nested_int(item.metrics, "tools", "tool_calls") for item in results),
        "llm_calls": sum(nested_int(item.metrics, "tokens", "llm_calls") for item in results),
        "prompt_tokens": sum(nested_int(item.metrics, "tokens", "prompt_tokens") for item in results),
        "total_tokens": sum(nested_int(item.metrics, "tokens", "total_tokens") for item in results),
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
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
    }


def _local_suite_names(value: str) -> list[str]:
    names = suite_names(value, ["cache", "memory", "latency"])
    unknown = sorted(set(names) - LOCAL_SUITES)
    if unknown:
        raise ValueError(f"Unknown local suite(s): {', '.join(unknown)}")
    return names


def _dry_run_plan(suites: list[str], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "runner": "memory_cache",
        "suites": suites,
        "output_root": str(Path(args.output_root)),
        "cache_turns": args.cache_turns,
        "memory_tasks": len(load_task_list("memory_ab.json")["tasks"]),
        "memory_limit": args.memory_limit,
        "latency_tasks": len(load_task_list("latency_smoke.json")["tasks"]),
        "latency_limit": args.latency_limit,
    }


def _safe_name(value: str) -> str:
    from eval.scripts.eval_common import safe_name

    return safe_name(value)


def run_self_test() -> None:
    temp_dir = tempfile.mkdtemp()
    try:
        assert len(load_task_list("memory_ab.json")["tasks"]) >= 5
        assert len(load_task_list("latency_smoke.json")["tasks"]) >= 10
        root = Path(temp_dir) / "results"
        for suite in ("memory_ab", "latency"):
            run_dir = root / suite
            run_dir.mkdir(parents=True)
            (run_dir / "summary.json").write_text(
                json.dumps({"suite": suite, "task_count": 1, "passed": 1, "pass_rate": 1.0}),
                encoding="utf-8",
            )
        write_eval_reports(root, report_output_dir=Path(temp_dir) / "out")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
