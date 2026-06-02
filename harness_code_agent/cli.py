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
    if len(argv) >= 2 and argv[:2] == ["session", "observe"]:
        return observe_session(Path.cwd(), argv[2:])

    parser = argparse.ArgumentParser(prog="hca", description="Interactive local coding agent")
    parser.add_argument("task", nargs="*", help="Optional first task to submit after startup")
    parser.add_argument("--profile", default=PRODUCT_DEFAULT_PROFILE, help="Profile name to use before the session starts")
    parser.add_argument("--resume", help="Session id to resume as context")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true",
        help="Execute a single task and print results (no REPL)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--list-profiles", action="store_true", help="List profiles and exit")
    args = parser.parse_args(argv)
    profile_explicit = any(arg == "--profile" or arg.startswith("--profile=") for arg in argv)

    from .core.logging_config import setup_logging
    setup_logging(verbose=args.verbose)

    if args.list_profiles:
        from .core.interactive import print_profiles
        print_profiles()
        return 0

    first_task = " ".join(args.task).strip()

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        return 1

    cwd = Path.cwd()
    is_tty = _is_interactive_tty()

    if args.print_mode or not is_tty:
        if not first_task and not args.print_mode:
            # Only read stdin when auto-degrading (no TTY). -p requires argv.
            first_task = sys.stdin.read().strip()
        if not first_task:
            print("Error: no task provided (pass a task argument or pipe input)")
            return 2
        try:
            stream_sink = _build_stream_callback()
        except ValueError as e:
            print(f"Error: {e}")
            return 2
        return run_batch(
            cwd=cwd,
            profile_name=args.profile,
            profile_explicit=profile_explicit,
            resume_session_id=args.resume,
            first_task=first_task,
            stream_sink=stream_sink,
        )

    try:
        app = TuiApp(
            cwd=cwd,
            profile_name=args.profile,
            profile_explicit=profile_explicit,
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
    profile_explicit: bool = False,
) -> int:
    try:
        session = InteractiveSession(
            cwd=cwd,
            profile_name=profile_name,
            profile_explicit=profile_explicit,
            resume_session_id=resume_session_id,
            stream_sink=stream_sink,
        )
    except Exception as e:
        print(f"Error: {e}")
        return 1

    try:
        result = session.submit(first_task)
        if session.session_id:
            print(f"hca session: {session.session_id}")
        print(f"workspace: {session.cwd}")
        print_turn_result(result)
    except MentionResolutionError as e:
        print(f"Error: {e}")
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


def observe_session(cwd: Path, args: list[str]) -> int:
    from .sessions.observability import (
        export_observability_report,
        format_export_result,
        format_project_observability,
        format_session_observability,
    )

    export = False
    target_args = list(args)
    if "--export" in target_args:
        export = True
        target_args.remove("--export")
    if len(target_args) != 1:
        print("Error: Usage: hca session observe latest|<session-id>|project [--export]")
        return 2

    target = target_args[0]
    store = SessionStore(cwd / ".harness")
    try:
        if target == "project":
            if export:
                print(format_export_result(export_observability_report(store, mode="project")))
            else:
                print(format_project_observability(store))
            return 0

        session_id = store.latest_session()["id"] if target == "latest" else target
        if export:
            print(format_export_result(export_observability_report(store, mode="current", session_id=session_id)))
        else:
            print(format_session_observability(store, session_id))
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
