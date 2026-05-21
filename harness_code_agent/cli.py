"""hca command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .core.interactive import InteractiveSession, PRODUCT_DEFAULT_PROFILE, print_session, print_turn_result
from .core.mentions import MentionResolutionError
from .sessions.store import SessionStore
from .tui import TuiApp


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        print("Error: 'run' is no longer supported. Use 'hca \"<task>\"' or start 'hca' interactively.")
        return 1
    if argv == ["session", "show", "latest"]:
        return show_latest_session(Path.cwd())

    parser = argparse.ArgumentParser(prog="hca", description="Interactive local coding agent")
    parser.add_argument("task", nargs="*", help="Optional first task to submit after startup")
    parser.add_argument("--profile", default=PRODUCT_DEFAULT_PROFILE, help="Profile name to use before the session starts")
    parser.add_argument("--resume", help="Session id to resume as context")
    parser.add_argument(
        "--exit-after-task",
        "--no-repl",
        dest="exit_after_task",
        action="store_true",
        help="Submit the task and exit instead of entering the REPL",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--list-profiles", action="store_true", help="List profiles and exit")
    args = parser.parse_args(argv)

    from .core.logging_config import setup_logging
    setup_logging(verbose=args.verbose)

    if args.list_profiles:
        from .core.interactive import print_profiles
        print_profiles()
        return 0

    first_task = " ".join(args.task).strip()
    if args.exit_after_task and not first_task:
        print("Error: --exit-after-task/--no-repl requires a task.")
        return 2

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        return 1

    if args.exit_after_task:
        try:
            stream_sink = _build_stream_callback()
        except ValueError as e:
            print(f"Error: {e}")
            return 2
        return run_batch(
            cwd=Path.cwd(),
            profile_name=args.profile,
            resume_session_id=args.resume,
            first_task=first_task,
            stream_sink=stream_sink,
        )

    if not _is_interactive_tty():
        print("Error: hca interactive mode requires a TTY. Use --exit-after-task/--no-repl for batch execution.")
        return 2

    try:
        app = TuiApp(
            cwd=Path.cwd(),
            profile_name=args.profile,
            resume_session_id=args.resume,
            first_task=first_task,
        )
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return app.run()


def run_batch(
    *,
    cwd: Path,
    profile_name: str,
    resume_session_id: str | None,
    first_task: str,
    stream_sink=None,
) -> int:
    try:
        session = InteractiveSession(
            cwd=cwd,
            profile_name=profile_name,
            resume_session_id=resume_session_id,
            stream_sink=stream_sink,
        )
    except Exception as e:
        print(f"Error: {e}")
        return 1

    print(f"hca session: {session.session.id}")
    print(f"workspace: {session.cwd}")

    try:
        _submit_and_print(session, first_task)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        session.close()
    return 0


def _is_interactive_tty() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def _build_stream_callback():
    value = (config.STREAM or "auto").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        enabled = True
    elif value in {"0", "false", "no", "off"}:
        enabled = False
    elif value == "auto":
        enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())
    else:
        raise ValueError("HARNESS_STREAM must be auto, 1, or 0")
    if not enabled:
        return None

    def stream_callback(delta: str) -> None:
        sys.stdout.write(delta)
        sys.stdout.flush()

    return stream_callback


def show_latest_session(cwd: Path) -> int:
    store = SessionStore(cwd / ".harness")
    try:
        latest = store.latest_session()
        print_session(store, latest["id"])
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"Error: {e}")
        return 1
    return 0


def _submit_and_print(session: InteractiveSession, line: str) -> None:
    try:
        result = session.submit(line)
    except MentionResolutionError as e:
        print(f"Error: {e}")
        return
    print_turn_result(result)



if __name__ == "__main__":
    raise SystemExit(main())
