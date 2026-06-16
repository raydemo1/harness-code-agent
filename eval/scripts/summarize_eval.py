"""Summarize lightweight agent eval outputs into resume-ready reports."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "eval"


@dataclass
class EvalSummary:
    generated_at: str
    result_root: str
    cache: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    tbench: dict[str, Any] = field(default_factory=dict)
    claw: dict[str, Any] = field(default_factory=dict)
    source_runs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "result_root": self.result_root,
            "cache": self.cache,
            "memory": self.memory,
            "latency": self.latency,
            "tbench": self.tbench,
            "claw": self.claw,
            "source_runs": list(self.source_runs),
        }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0

    summary = summarize_result_root(Path(args.results_root))
    write_reports(summary, output_dir=Path(args.output_dir))
    print(f"Wrote eval reports to: {Path(args.output_dir).resolve()}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize eval results for resume reporting.")
    parser.add_argument("--results-root", default=str(DEFAULT_RESULTS_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def summarize_result_root(result_root: str | Path) -> EvalSummary:
    root = Path(result_root)
    summary = EvalSummary(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        result_root=str(_display_path(root.resolve())),
    )
    if not root.exists():
        return summary

    for path in sorted(root.glob("*/summary.json"), key=lambda item: item.stat().st_mtime):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        suite = _suite_name(payload, path.parent.name)
        summary.source_runs.append(str(_display_path(path.parent)))
        if suite == "cache":
            summary.cache = _merge_cache_summary(summary.cache, _cache_summary(payload, path.parent.name))
        elif suite == "memory_ab":
            summary.memory = _memory_summary(payload, path.parent.name)
        elif suite == "latency":
            summary.latency = _latency_summary(payload, path.parent.name)
        elif suite == "tbench":
            summary.tbench = _tbench_summary(payload, path.parent.name)
        elif suite == "claw_swe_bench":
            summary.claw = _claw_summary(payload, path.parent.name)
    return summary


def write_reports(summary: EvalSummary, *, output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_summary.json").write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (out / "report_internal.md").write_text(
        render_internal_report(summary),
        encoding="utf-8",
        newline="\n",
    )
    (out / "report_resume.md").write_text(
        render_resume_report(summary),
        encoding="utf-8",
        newline="\n",
    )


def render_internal_report(summary: EvalSummary) -> str:
    lines = [
        "# Agent Eval Internal Report",
        "",
        f"Generated at: {summary.generated_at}",
        f"Result root: {summary.result_root}",
        "",
        "## Sources",
    ]
    lines.extend(f"- {item}" for item in summary.source_runs) if summary.source_runs else lines.append("- none")
    lines.extend([
        "",
        "## Metrics",
        "",
        "| Area | Key Result |",
        "| --- | --- |",
        f"| Context cache | {_cache_line(summary.cache)} |",
        f"| Memory A/B | {_memory_line(summary.memory)} |",
        f"| Latency | {_latency_line(summary.latency)} |",
        f"| {_tbench_label(summary.tbench)} | {_tbench_line(summary.tbench)} |",
        f"| {_claw_label(summary.claw)} | {_claw_line(summary.claw)} |",
        "",
    ])
    return "\n".join(lines)


def render_resume_report(summary: EvalSummary) -> str:
    bullets = [
        "- Built a lightweight evaluation harness for a local coding agent, with fixed task definitions for DeepSeek cache efficiency, memory A/B, latency, and Terminal-Bench subsets.",
    ]
    if summary.cache:
        bullets.append(
            f"- Measured DeepSeek prompt-cache warmup from cold start to {_percent(_number(summary.cache.get('stable_last_hit_ratio')))} on stable multi-turn project context."
        )
    if summary.memory:
        bullets.append("- Measured memory-enabled runs against disabled baselines, reporting tool-call, elapsed-time, and token deltas.")
    if summary.latency:
        bullets.append("- Reported latency p50/p95/p99 from completed evaluation runs.")
    if summary.tbench:
        bullets.append(f"- Reported {_tbench_label(summary.tbench)} pass-rate results from completed benchmark runs.")
    if summary.claw:
        bullets.append(f"- Added Claw-SWE-Bench reporting with patch-generation and token telemetry for SWE-style harness evaluation.")

    lines = [
        "# Resume-Ready Agent Eval Report",
        "",
        "## One-Page Metrics",
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| DeepSeek context cache | {_cache_line(summary.cache)} |",
        f"| Memory A/B | {_memory_line(summary.memory)} |",
        f"| Latency p95/p99 | {_latency_line(summary.latency)} |",
        f"| {_tbench_label(summary.tbench)} | {_tbench_line(summary.tbench)} |",
        f"| {_claw_label(summary.claw)} | {_claw_line(summary.claw)} |",
        "",
        "## Mechanism Effects",
        "",
        f"- Cache: {_cache_line(summary.cache)}",
        f"- Memory: {_memory_line(summary.memory)}",
        f"- Compaction: {_compaction_line(summary.cache)}",
        "",
        "## Resume Bullets",
        "",
        *bullets,
        "",
    ]
    return "\n".join(lines)


def _suite_name(payload: dict[str, Any], dirname: str) -> str:
    explicit = str(payload.get("suite") or "").strip()
    if explicit:
        return explicit
    if "scenarios" in payload:
        return "cache"
    lowered = dirname.lower()
    if "memory" in lowered:
        return "memory_ab"
    if "latency" in lowered:
        return "latency"
    if "tbench" in lowered or "terminal" in lowered:
        return "tbench"
    if "claw" in lowered:
        return "claw_swe_bench"
    return "unknown"


def _cache_summary(payload: dict[str, Any], run_name: str) -> dict[str, Any]:
    scenarios = payload.get("scenarios") or {}
    stable = scenarios.get("stable_warmup") or {}
    compaction = scenarios.get("compaction_rewrite") or {}
    return {
        "run": run_name,
        "stable_first_hit_ratio": _number(stable.get("first_hit_ratio")),
        "stable_last_hit_ratio": _number(stable.get("last_hit_ratio")),
        "stable_avg_hit_ratio": _number(stable.get("avg_hit_ratio")),
        "compaction_last_hit_ratio": _number(compaction.get("last_hit_ratio")),
        "prefix_changes": compaction.get("prefix_changes") or [],
    }


def _merge_cache_summary(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return dict(incoming)
    merged = dict(existing)
    if _number(incoming.get("stable_last_hit_ratio")) > 0:
        for key in ("stable_first_hit_ratio", "stable_last_hit_ratio", "stable_avg_hit_ratio"):
            merged[key] = incoming.get(key)
        merged["stable_run"] = incoming.get("run")
    if incoming.get("prefix_changes") or _number(incoming.get("compaction_last_hit_ratio")) > 0:
        merged["compaction_last_hit_ratio"] = incoming.get("compaction_last_hit_ratio")
        merged["prefix_changes"] = incoming.get("prefix_changes") or []
        merged["compaction_run"] = incoming.get("run")
    merged.setdefault("run", incoming.get("run") or existing.get("run"))
    return merged


def _memory_summary(payload: dict[str, Any], run_name: str) -> dict[str, Any]:
    uplift = payload.get("uplift") or {}
    return {
        "run": run_name,
        "task_count": _int(payload.get("task_count")),
        "tool_calls_reduction_ratio": _number(uplift.get("tool_calls_reduction_ratio")),
        "elapsed_seconds_reduction_ratio": _number(uplift.get("elapsed_seconds_reduction_ratio")),
        "total_tokens_reduction_ratio": _number(uplift.get("total_tokens_reduction_ratio")),
        "success_delta": _number(uplift.get("success_delta")),
    }


def _latency_summary(payload: dict[str, Any], run_name: str) -> dict[str, Any]:
    return {
        "run": run_name,
        "turn_duration_ms": payload.get("turn_duration_ms") or {},
        "llm_response_latency_ms": payload.get("llm_response_latency_ms") or {},
        "llm_first_token_ms": payload.get("llm_first_token_ms") or {},
    }


def _tbench_summary(payload: dict[str, Any], run_name: str) -> dict[str, Any]:
    return {
        "run": run_name,
        "benchmark_name": str(payload.get("benchmark_name") or "Terminal-Bench 2.0 subset"),
        "task_set": str(payload.get("task_set") or "subset"),
        "task_count": _int(payload.get("task_count")),
        "passed": _int(payload.get("passed")),
        "pass_rate": _number(payload.get("pass_rate")),
        "status": str(payload.get("status") or "unknown"),
        "category_results": payload.get("category_results") or {},
    }


def _claw_summary(payload: dict[str, Any], run_name: str) -> dict[str, Any]:
    token_totals = payload.get("token_totals") or {}
    return {
        "run": run_name,
        "benchmark_name": str(payload.get("benchmark_name") or "Claw-SWE-Bench"),
        "task_set": str(payload.get("task_set") or "subset"),
        "task_count": _int(payload.get("task_count")),
        "patch_collected": _int(payload.get("patch_collected")),
        "failed": _int(payload.get("failed")),
        "timed_out": _int(payload.get("timed_out")),
        "patch_empty": _int(payload.get("patch_empty")),
        "patch_collection_rate": _number(payload.get("patch_collection_rate")),
        "model": str(payload.get("model") or ""),
        "token_totals": token_totals,
    }


def _cache_line(data: dict[str, Any]) -> str:
    if not data:
        return "not run"
    return (
        f"warmup {_percent(_number(data.get('stable_first_hit_ratio')))} -> "
        f"{_percent(_number(data.get('stable_last_hit_ratio')))}"
    )


def _memory_line(data: dict[str, Any]) -> str:
    if not data:
        return "not run"
    return (
        f"{_int(data.get('task_count'))} tasks; tool calls -{_percent(_number(data.get('tool_calls_reduction_ratio')))}, "
        f"elapsed -{_percent(_number(data.get('elapsed_seconds_reduction_ratio')))}, "
        f"tokens -{_percent(_number(data.get('total_tokens_reduction_ratio')))}"
    )


def _latency_line(data: dict[str, Any]) -> str:
    if not data:
        return "not run"
    turn = data.get("turn_duration_ms") or {}
    response = data.get("llm_response_latency_ms") or {}
    ttft = data.get("llm_first_token_ms") or {}
    return (
        f"turn p95={_int(turn.get('p95'))}ms p99={_int(turn.get('p99'))}ms; "
        f"LLM p95={_int(response.get('p95'))}ms; TTFT p95={_int(ttft.get('p95'))}ms"
    )


def _tbench_line(data: dict[str, Any]) -> str:
    if not data:
        return "not run"
    category_detail = _category_line(data.get("category_results") or {})
    category_suffix = f"; categories: {category_detail}" if category_detail else ""
    task_set = str(data.get("task_set") or "subset")
    return (
        f"{_int(data.get('passed'))}/{_int(data.get('task_count'))} passed "
        f"({_percent(_number(data.get('pass_rate')))}), {task_set}{category_suffix}"
    )


def _tbench_label(data: dict[str, Any]) -> str:
    if not data:
        return "Terminal-Bench 2.0 subset"
    return str(data.get("benchmark_name") or "Terminal-Bench 2.0 subset")


def _claw_line(data: dict[str, Any]) -> str:
    if not data:
        return "not run"
    tokens = data.get("token_totals") or {}
    token_suffix = ""
    total_tokens = _int(tokens.get("total_tokens"))
    if total_tokens:
        token_suffix = f"; tokens={total_tokens}"
    model = str(data.get("model") or "").strip()
    model_suffix = f"; model={model}" if model else ""
    return (
        f"{_int(data.get('patch_collected'))}/{_int(data.get('task_count'))} patches "
        f"({_percent(_number(data.get('patch_collection_rate')))}), "
        f"empty={_int(data.get('patch_empty'))}, {data.get('task_set')}"
        f"{model_suffix}{token_suffix}"
    )


def _claw_label(data: dict[str, Any]) -> str:
    if not data:
        return "Claw-SWE-Bench"
    return str(data.get("benchmark_name") or "Claw-SWE-Bench")


def _category_line(category_results: dict[str, Any]) -> str:
    if not isinstance(category_results, dict):
        return ""
    parts: list[str] = []
    for category, payload in sorted(category_results.items()):
        if not isinstance(payload, dict):
            continue
        parts.append(f"{category} {_int(payload.get('passed'))}/{_int(payload.get('task_count'))}")
    return ", ".join(parts)


def _compaction_line(data: dict[str, Any]) -> str:
    if not data:
        return "not run"
    changes = data.get("prefix_changes") or []
    if not changes:
        return "no compaction rewrite data"
    reasons = sorted({reason for item in changes for reason in item.get("reasons", [])})
    return f"rewrite diagnosed via {', '.join(reasons) or 'prefix change'}; post-rewrite hit {_percent(_number(data.get('compaction_last_hit_ratio')))}"


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _display_path(path: Path) -> Path:
    try:
        return path.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        return path.resolve()


def run_self_test() -> None:
    temp_dir = tempfile.mkdtemp()
    try:
        root = Path(temp_dir) / "results"
        cache_dir = root / "cache"
        memory_dir = root / "memory"
        latency_dir = root / "latency"
        tbench_dir = root / "tbench"
        claw_dir = root / "claw"
        for path in (cache_dir, memory_dir, latency_dir, tbench_dir, claw_dir):
            path.mkdir(parents=True)
        (cache_dir / "summary.json").write_text(json.dumps({
            "scenarios": {"stable_warmup": {"first_hit_ratio": 0, "last_hit_ratio": 0.99}}
        }), encoding="utf-8")
        (memory_dir / "summary.json").write_text(json.dumps({
            "suite": "memory_ab",
            "task_count": 1,
            "uplift": {"tool_calls_reduction_ratio": 0.1},
        }), encoding="utf-8")
        (latency_dir / "summary.json").write_text(json.dumps({
            "suite": "latency",
            "turn_duration_ms": {"p95": 10, "p99": 20},
        }), encoding="utf-8")
        (tbench_dir / "summary.json").write_text(json.dumps({
            "suite": "tbench",
            "benchmark_name": "Terminal-Bench 2.0 8-task subset",
            "task_set": "8task",
            "task_count": 8,
            "passed": 6,
            "pass_rate": 0.75,
            "category_results": {"debugging": {"task_count": 3, "passed": 2, "pass_rate": 2 / 3}},
        }), encoding="utf-8")
        (claw_dir / "summary.json").write_text(json.dumps({
            "suite": "claw_swe_bench",
            "benchmark_name": "Claw-SWE-Bench Lite80",
            "task_set": "lite80",
            "task_count": 2,
            "patch_collected": 1,
            "patch_collection_rate": 0.5,
            "patch_empty": 1,
            "model": "deepseek-v4-flash",
            "token_totals": {"total_tokens": 123},
        }), encoding="utf-8")
        summary = summarize_result_root(root)
        out = Path(temp_dir) / "out"
        write_reports(summary, output_dir=out)
        assert (out / "report_resume.md").exists()
        report = (out / "report_resume.md").read_text(encoding="utf-8")
        assert "Terminal-Bench 2.0 8-task subset" in report
        assert "debugging 2/3" in report
        assert "Claw-SWE-Bench Lite80" in report
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    print("self-test passed")


if __name__ == "__main__":
    raise SystemExit(main())
