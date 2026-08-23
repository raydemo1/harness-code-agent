"""Usage and cost helpers for benchmark runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harness_code_agent.sessions.observability import build_session_observability
from harness_code_agent.sessions.store import SessionStore

EVAL_METRICS_PREFIX = "HCA_EVAL_METRICS:"

# Official DeepSeek API prices, per 1M tokens, checked from:
# https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_PRICING_PER_1M = {
    "deepseek-v4-flash": {
        "input_cache_hit": 0.0028,
        "input_cache_miss": 0.14,
        "output": 0.28,
    },
    "deepseek-v4-pro": {
        "input_cache_hit": 0.003625,
        "input_cache_miss": 0.435,
        "output": 0.87,
    },
}

def build_session_eval_metrics(store: SessionStore, session_id: str, *, model: str = "") -> dict[str, Any]:
    metadata = store.read_metadata(session_id)
    events = store.read_events(session_id)
    snapshot = build_session_observability(metadata, events)
    tokens = snapshot.tokens.to_dict()
    usage_cost = estimate_usage_cost(tokens, model=str(model or metadata.get("model") or ""))
    return {
        "schema_version": 1,
        "session_id": session_id,
        "model": str(model or metadata.get("model") or ""),
        "turns": _turn_metrics(events),
        "tokens": tokens,
        "tools": snapshot.tools.to_dict(),
        "performance": snapshot.performance.to_dict(),
        "audit": snapshot.audit.to_dict(),
        "usage_cost": usage_cost,
    }


def estimate_usage_cost(tokens: dict[str, Any], *, model: str) -> dict[str, Any]:
    pricing = deepseek_pricing_for_model(model)
    if pricing is None:
        return {
            "currency": "USD",
            "estimated_cost_usd": None,
            "pricing_source": None,
            "pricing_per_1m_tokens": None,
        }

    prompt_tokens = _int(tokens.get("prompt_tokens"))
    cached_tokens = _int(tokens.get("cached_tokens"))
    cache_miss_tokens = _int(tokens.get("cache_miss_tokens"))
    if cache_miss_tokens <= 0 and prompt_tokens > 0:
        cache_miss_tokens = max(0, prompt_tokens - cached_tokens)
    output_tokens = _int(tokens.get("completion_tokens"))

    estimated = (
        cached_tokens * pricing["input_cache_hit"]
        + cache_miss_tokens * pricing["input_cache_miss"]
        + output_tokens * pricing["output"]
    ) / 1_000_000
    return {
        "currency": "USD",
        "estimated_cost_usd": estimated,
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing",
        "pricing_per_1m_tokens": dict(pricing),
        "billable_tokens": {
            "input_cache_hit": cached_tokens,
            "input_cache_miss": cache_miss_tokens,
            "output": output_tokens,
        },
    }


def deepseek_pricing_for_model(model: str) -> dict[str, float] | None:
    normalized = str(model or "").strip().lower()
    if normalized in DEEPSEEK_PRICING_PER_1M:
        return _env_pricing_override(normalized) or DEEPSEEK_PRICING_PER_1M[normalized]
    return None


def print_eval_metrics(metrics: dict[str, Any]) -> None:
    print(EVAL_METRICS_PREFIX + json.dumps(metrics, ensure_ascii=False, sort_keys=True))


def parse_eval_metrics_from_text(text: str) -> dict[str, Any] | None:
    for line in reversed((text or "").splitlines()):
        if EVAL_METRICS_PREFIX not in line:
            continue
        payload = line.split(EVAL_METRICS_PREFIX, 1)[1].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def collect_harbor_job_usage(job_dir: Path) -> dict[str, Any]:
    task_results: list[dict[str, Any]] = []
    for result_path in sorted(job_dir.glob("*/result.json")):
        try:
            result_text = result_path.read_text(encoding="utf-8")
            payload = json.loads(result_text)
        except (OSError, json.JSONDecodeError):
            continue
        agent_result = payload.get("agent_result") or {}
        metadata = agent_result.get("metadata") or {}
        metrics = metadata.get("hca_eval_metrics") if isinstance(metadata, dict) else None
        if not isinstance(metrics, dict) or not metrics:
            metrics = _metrics_from_trial_outputs(result_path.parent, result_text, payload)
        artifact_session_id = _artifact_session_id(result_path.parent)
        session_id = (
            (metrics or {}).get("session_id")
            if isinstance(metrics, dict)
            else ""
        ) or artifact_session_id
        rewards = ((payload.get("verifier_result") or {}).get("rewards") or {})
        exception_info = payload.get("exception_info") if isinstance(payload.get("exception_info"), dict) else {}
        task_results.append({
            "task": _task_name(payload),
            "trial_name": payload.get("trial_name") or result_path.parent.name,
            "job_dir": str(job_dir),
            "trial_dir": str(result_path.parent),
            "passed": rewards.get("reward") == 1.0,
            "reward": rewards.get("reward"),
            "session_id": session_id,
            "has_hca_artifacts": bool(artifact_session_id),
            "metrics": metrics or {},
            "n_input_tokens": agent_result.get("n_input_tokens"),
            "n_cache_tokens": agent_result.get("n_cache_tokens"),
            "n_output_tokens": agent_result.get("n_output_tokens"),
            "cost_usd": agent_result.get("cost_usd"),
            "exception_type": str(exception_info.get("exception_type") or ""),
            "exception_message": str(exception_info.get("exception_message") or "")[:1000],
        })
    return {
        "job_dir": str(job_dir),
        "task_results": task_results,
        **aggregate_usage(task_results),
    }


def _metrics_from_trial_outputs(trial_dir: Path, result_text: str, payload: dict[str, Any]) -> dict[str, Any]:
    for text in _payload_text_fields(payload):
        metrics = parse_eval_metrics_from_text(text)
        if metrics:
            return metrics
    metrics = parse_eval_metrics_from_text(result_text)
    if metrics:
        return metrics
    for name in ("trial.log", "exception.txt"):
        path = trial_dir / name
        try:
            metrics = parse_eval_metrics_from_text(path.read_text(encoding="utf-8"))
        except OSError:
            metrics = None
        if metrics:
            return metrics
    return {}


def _artifact_session_id(trial_dir: Path) -> str:
    hca_root = trial_dir / "artifacts" / "hca"
    if not hca_root.is_dir():
        return ""
    sessions = sorted(
        (path for path in hca_root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for session_dir in sessions:
        if (session_dir / "manifest.json").exists() or (session_dir / "early_manifest.json").exists():
            return session_dir.name
    return sessions[0].name if sessions else ""


def _payload_text_fields(payload: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for section_name in ("exception_info", "agent_result"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        for value in section.values():
            if isinstance(value, str):
                fields.append(value)
    return fields


def aggregate_usage(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    token_totals = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "cache_miss_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    turns = {
        "started": 0,
        "finished": 0,
    }
    tool_calls = 0
    total_cost: float | None = None
    for item in task_results:
        metrics = item.get("metrics") or {}
        tokens = metrics.get("tokens") or {}
        for key in token_totals:
            token_totals[key] += _int(tokens.get(key))
        turn_metrics = metrics.get("turns") or {}
        turns["started"] += _int(turn_metrics.get("started"))
        turns["finished"] += _int(turn_metrics.get("finished"))
        tool_calls += _int((metrics.get("tools") or {}).get("tool_calls"))
        cost = (metrics.get("usage_cost") or {}).get("estimated_cost_usd")
        if cost is not None:
            total_cost = (total_cost or 0.0) + float(cost)
    return {
        "token_totals": token_totals,
        "turn_totals": turns,
        "tool_calls": tool_calls,
        "estimated_cost_usd": total_cost,
    }


def _turn_metrics(events: list[dict[str, Any]]) -> dict[str, int]:
    started = sum(1 for event in events if event.get("type") == "turn_started")
    finished = sum(1 for event in events if event.get("type") == "turn_finished")
    return {
        "started": started,
        "finished": finished,
    }


def _task_name(payload: dict[str, Any]) -> str:
    task_name = str(payload.get("task_name") or "")
    if "/" in task_name:
        return task_name.rsplit("/", 1)[-1]
    trial_name = str(payload.get("trial_name") or "")
    return trial_name.split("__", 1)[0] if "__" in trial_name else trial_name


def _env_pricing_override(model: str) -> dict[str, float] | None:
    prefix = "HARNESS_DEEPSEEK_" + model.upper().replace("-", "_") + "_"
    keys = {
        "input_cache_hit": prefix + "INPUT_CACHE_HIT_PER_1M",
        "input_cache_miss": prefix + "INPUT_CACHE_MISS_PER_1M",
        "output": prefix + "OUTPUT_PER_1M",
    }
    if not any(name in os.environ for name in keys.values()):
        return None
    base = dict(DEEPSEEK_PRICING_PER_1M[model])
    for key, env_name in keys.items():
        if env_name in os.environ:
            base[key] = float(os.environ[env_name])
    return base


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
