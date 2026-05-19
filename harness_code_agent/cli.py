"""hca command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .core.interactive import InteractiveSession, PRODUCT_DEFAULT_PROFILE, print_turn_result
from .core.mentions import MentionResolutionError


SLASH_COMMANDS = [
    "/help",
    "/sessions",
    "/session",
    "/resume",
    "/fork",
    "/rollback",
    "/profiles",
    "/code",
    "/plan",
    "/terminal",
    "/swe",
    "/app",
    "/doctor",
    "/config show",
    "/checkpoint",
    "/exit",
]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        print("Error: 'run' is no longer supported. Use 'hca \"<task>\"' or start 'hca' interactively.")
        return 1

    parser = argparse.ArgumentParser(prog="hca", description="Interactive local coding agent")
    parser.add_argument("task", nargs="*", help="Optional first task to submit after startup")
    parser.add_argument("--profile", default=PRODUCT_DEFAULT_PROFILE, help="Profile name to use before the session starts")
    parser.add_argument("--resume", help="Session id to resume as context")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--list-profiles", action="store_true", help="List profiles and exit")
    args = parser.parse_args(argv)

    from .core.logging_config import setup_logging
    setup_logging(verbose=args.verbose)

    if args.list_profiles:
        from .core.interactive import print_profiles
        print_profiles()
        return 0

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        return 1

    try:
        session = InteractiveSession(
            cwd=Path.cwd(),
            profile_name=args.profile,
            resume_session_id=args.resume,
        )
    except Exception as e:
        print(f"Error: {e}")
        return 1

    print(f"hca session: {session.session.id}")
    print(f"workspace: {session.cwd}")
    print("Type /help for commands, /exit to quit.")

    first_task = " ".join(args.task).strip()
    try:
        if first_task:
            _submit_and_print(session, first_task)
        repl(session)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        session.close()
    return 0


def repl(session: InteractiveSession) -> None:
    prompt = _build_prompt(session)
    while True:
        try:
            line = prompt()
        except EOFError:
            print()
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            should_continue = session.handle_slash_command(line)
            if not should_continue:
                break
            continue
        _submit_and_print(session, line)


def _submit_and_print(session: InteractiveSession, line: str) -> None:
    try:
        result = session.submit(line)
    except MentionResolutionError as e:
        print(f"Error: {e}")
        return
    print_turn_result(result)


def _build_prompt(session: InteractiveSession):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
    except ImportError:
        def simple_prompt() -> str:
            return input("hca> ")
        return simple_prompt

    class HcaCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            word = document.get_word_before_cursor(WORD=True)
            if text.lstrip().startswith("/"):
                prefix = text.strip()
                for command in SLASH_COMMANDS:
                    if command.startswith(prefix):
                        yield Completion(command, start_position=-len(prefix))
                return
            if "@session:" in text:
                prefix = word.removeprefix("@session:")
                for item in session.session_store.list_sessions()[:20]:
                    session_id = item.get("id", "")
                    if session_id.startswith(prefix):
                        yield Completion(session_id, start_position=-len(prefix))
                return
            if word.startswith("@"):
                prefix = word[1:]
                for path in _iter_file_completions(session.cwd, prefix):
                    yield Completion(path, start_position=-len(prefix))

    prompt_session = PromptSession(completer=HcaCompleter())

    def prompt() -> str:
        return prompt_session.prompt("hca> ")

    return prompt


def _iter_file_completions(root: Path, prefix: str):
    base = root / prefix
    parent = base.parent if prefix else root
    if not parent.exists() or not parent.is_dir():
        return
    stem = base.name
    for item in sorted(parent.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if item.name.startswith(".") and item.name != ".env.template":
            continue
        if not item.name.startswith(stem):
            continue
        try:
            rel = item.relative_to(root).as_posix()
        except ValueError:
            continue
        if item.is_dir():
            rel += "/"
        yield rel



if __name__ == "__main__":
    raise SystemExit(main())
