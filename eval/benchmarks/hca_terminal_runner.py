"""Headless in-container runner used by the Terminal-Bench Harbor adapter."""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = Path(args.workspace).resolve()

    os.environ.setdefault("HARNESS_WORKSPACE", str(workspace))
    os.environ.setdefault("HARNESS_PERMISSION_MODE", "danger-full-access")
    os.environ.setdefault("HARNESS_STREAM", "0")
    os.environ.setdefault("HARNESS_MEMORY_DISABLED", "1")
    os.environ.setdefault("HARNESS_MEMORY_DREAM_CHECK_INTERVAL_SECONDS", "3600")

    try:
        from harness_code_agent.core.interactive import InteractiveSession, print_turn_result
        from eval.benchmarks.usage_metrics import build_session_eval_metrics, print_eval_metrics

        session = InteractiveSession(
            cwd=workspace,
            profile_name="terminal",
            profile_explicit=True,
            stream_sink=None,
        )
        session.checkpoint.auto = False
        try:
            result = session.submit(args.prompt)
            session_id = session.session_id
            session_store = session.session_store
            if session.session_id:
                print(f"hca session: {session.session_id}")
            print(f"workspace: {session.cwd}")
            print_turn_result(result)
        finally:
            session.close()
        if session_id:
            metrics = build_session_eval_metrics(
                session_store,
                session_id,
                model=os.environ.get("HARNESS_MODEL", ""),
            )
            print_eval_metrics(metrics)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run harness-code-agent on a Terminal-Bench prompt.")
    parser.add_argument("prompt")
    parser.add_argument("--workspace", default="/app")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
