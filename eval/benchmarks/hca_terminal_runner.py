"""Headless in-container runner used by the Terminal-Bench Harbor adapter."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any


def export_session_artifacts(
    *,
    harness_root: str | Path,
    session_id: str,
    artifacts_root: str | Path = "/logs/artifacts",
    runner_error: str = "",
) -> Path:
    """Export the complete durable session and readable trajectory views."""
    harness_root = Path(harness_root).resolve()
    artifacts_root = Path(artifacts_root)
    session_root = harness_root / "sessions" / session_id
    if not session_root.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_root}")

    export_root = artifacts_root / "hca" / session_id
    if export_root.exists():
        shutil.rmtree(export_root)
    export_root.mkdir(parents=True, exist_ok=True)

    shutil.copytree(session_root, export_root / "session")

    observations_root = harness_root / "observations" / session_id
    observations_exported = observations_root.is_dir()
    if observations_exported:
        shutil.copytree(observations_root, export_root / "observations")

    traces_root = harness_root / "traces"
    traces_exported = traces_root.is_dir()
    if traces_exported:
        shutil.copytree(traces_root, export_root / "traces")

    runner_error = str(runner_error or "").strip()
    runner_error_exported = bool(runner_error)
    if runner_error_exported:
        (export_root / "runner_error.txt").write_text(
            runner_error + "\n",
            encoding="utf-8",
        )

    events_path = session_root / "events.jsonl"
    events = _read_jsonl(events_path)
    _write_jsonl(export_root / "trajectory.jsonl", events)

    plan_events = [
        event
        for event in events
        if event.get("type") in {"tool_call", "tool_result"}
        and (event.get("payload") or {}).get("tool") == "update_plan_state"
    ]
    _write_jsonl(export_root / "plan_history.jsonl", plan_events)

    manifest = {
        "session_id": session_id,
        "event_count": len(events),
        "plan_event_count": len(plan_events),
        "observations_exported": observations_exported,
        "traces_exported": traces_exported,
        "runner_error_exported": runner_error_exported,
        "session_path": "session",
        "trajectory_path": "trajectory.jsonl",
        "plan_history_path": "plan_history.jsonl",
        "observations_path": "observations" if observations_exported else None,
        "traces_path": "traces" if traces_exported else None,
        "runner_error_path": "runner_error.txt" if runner_error_exported else None,
    }
    (export_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return export_root


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = Path(args.workspace).resolve()

    os.environ.setdefault("HARNESS_WORKSPACE", str(workspace))
    os.environ.setdefault("HARNESS_PERMISSION_MODE", "danger-full-access")
    os.environ.setdefault("HARNESS_STREAM", "0")
    os.environ.setdefault("HARNESS_MEMORY_DISABLED", "1")
    os.environ.setdefault("HARNESS_MEMORY_DREAM_CHECK_INTERVAL_SECONDS", "3600")

    session: Any | None = None
    session_store: Any | None = None
    session_id = ""
    result: Any | None = None
    session_started = False
    runner_error = ""
    try:
        from harness_code_agent.core.interactive import InteractiveSession, print_turn_result
        from eval.benchmarks.usage_metrics import build_session_eval_metrics, print_eval_metrics

        session = InteractiveSession(
            cwd=workspace,
            profile_name="terminal",
            profile_explicit=True,
            stream_sink=None,
        )
        session_started = True
        session.checkpoint.auto = False
        try:
            session_id = session.session_id
            session_store = session.session_store
            result = session.submit(args.prompt)
        except Exception:
            runner_error = traceback.format_exc()
            print(runner_error, file=sys.stderr, end="")
        finally:
            session_id = session_id or session.session_id
            session_store = session_store or session.session_store
            if session.session_id:
                print(f"hca session: {session.session_id}")
            print(f"workspace: {session.cwd}")
            if result is not None:
                print_turn_result(result)
            try:
                session.close()
            except Exception:
                close_error = traceback.format_exc()
                runner_error = "\n".join(
                    part for part in (runner_error.strip(), close_error.strip()) if part
                )
                print(close_error, file=sys.stderr, end="")
            if session_store is not None and session_id:
                try:
                    artifact_path = export_session_artifacts(
                        harness_root=session_store.root,
                        session_id=session_id,
                        artifacts_root=os.environ.get("HCA_ARTIFACTS_ROOT", "/logs/artifacts"),
                        runner_error=runner_error,
                    )
                    print(f"hca artifacts: {artifact_path}")
                except Exception:
                    print("Failed to export HCA session artifacts:", file=sys.stderr)
                    traceback.print_exc()
        if session_store is not None and session_id:
            metrics = build_session_eval_metrics(
                session_store,
                session_id,
                model=os.environ.get("HARNESS_MODEL", ""),
            )
            print_eval_metrics(metrics)
    except Exception:
        traceback.print_exc()
        return 1
    if session_started:
        return 0
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run harness-code-agent on a Terminal-Bench prompt.")
    parser.add_argument("prompt")
    parser.add_argument("--workspace", default="/app")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
