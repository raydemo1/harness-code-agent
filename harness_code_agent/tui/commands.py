from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CommandResult:
    text: str = ""
    should_continue: bool = True


CommandHandler = Callable[[Any, list[str], "SlashCommandRegistry"], CommandResult]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    group: str
    usage: str
    description: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class SlashCommandRegistry:
    def __init__(self, specs: list[CommandSpec]):
        self.specs = specs
        self._by_name = {
            name: spec
            for spec in specs
            for name in spec.names()
        }

    def execute(self, line: str, session: Any) -> CommandResult:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return CommandResult(f"Error: {exc}")
        if not parts:
            return CommandResult()
        command, args = parts[0], parts[1:]
        spec = self._by_name.get(command)
        if spec is None:
            return CommandResult(f"Unknown slash command: {command}")
        try:
            return spec.handler(session, args, self)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            return CommandResult(f"Error: {exc}")

    def command_names(self) -> list[str]:
        return [spec.name for spec in self.specs]

    def candidates(self) -> list[CommandSpec]:
        return list(self.specs)

    def format_help(self) -> str:
        lines = ["hca commands:"]
        current_group = ""
        for spec in self.specs:
            if spec.group != current_group:
                current_group = spec.group
                lines.append("")
                lines.append(f"{current_group}:")
            lines.append(f"  {spec.usage:45s} {spec.description}")
        return "\n".join(lines)


def default_command_registry() -> SlashCommandRegistry:
    return SlashCommandRegistry([
        CommandSpec("/help", "General", "/help", "Show commands grouped by workflow.", _help),
        CommandSpec("/exit", "General", "/exit", "Close the current TUI session.", _exit, aliases=("/quit",)),
        CommandSpec("/profiles", "Profiles", "/profiles", "List available profiles.", _profiles),
        CommandSpec("/code", "Profiles", "/code", "Switch to coding-agent profile.", _profile("coding-agent", "/code")),
        CommandSpec("/plan", "Profiles", "/plan", "Switch to constrained planning profile.", _profile("plan", "/plan")),
        CommandSpec("/terminal", "Profiles", "/terminal", "Switch to terminal task profile.", _profile("terminal", "/terminal")),
        CommandSpec("/swe", "Profiles", "/swe", "Switch to swe-bench profile.", _profile("swe-bench", "/swe")),
        CommandSpec("/app", "Profiles", "/app", "Switch to app-builder profile.", _profile("app-builder", "/app")),
        CommandSpec("/sessions", "Sessions", "/sessions", "List local Harness sessions.", _sessions),
        CommandSpec("/session", "Sessions", "/session <session-id>", "Show a human-readable session summary.", _session),
        CommandSpec("/resume", "Sessions", "/resume <session-id>", "Inject previous session context into this conversation.", _resume),
        CommandSpec("/fork", "Sessions", "/fork <session-id>", "Create a lineage-preserving fork record.", _fork),
        CommandSpec("/rollback", "Sessions", "/rollback <session-id> <path>", "Restore one file from the latest session snapshot.", _rollback),
        CommandSpec("/checkpoint", "Workflow", "/checkpoint [auto on|auto off|every turn|every <N> turns|status]", "Create or configure checkpoint commits.", _checkpoint),
        CommandSpec("/mcp", "Diagnostics", "/mcp [status|list|reload]", "Show or reload configured MCP servers and tools.", _mcp),
        CommandSpec("/doctor", "Diagnostics", "/doctor", "Check API, workspace, git, and shell setup.", _doctor),
        CommandSpec("/config", "Diagnostics", "/config show", "Show effective Harness configuration.", _config),
        CommandSpec("/observe", "Diagnostics", "/observe [current|project|export current|export project]", "Show or export observability metrics.", _observe),
        CommandSpec("/compact", "Workflow", "/compact show", "View the latest compacted summary.", _compact),
    ])


def _help(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /help")
    return CommandResult(registry.format_help())


def _exit(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /exit")
    return CommandResult(should_continue=False)


def _sessions(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /sessions")
    from ..core.interactive import format_sessions

    return CommandResult(format_sessions(session.session_store))


def _session(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _require_arg(args, "Usage: /session <session-id>")
    from ..sessions.summary import load_session_summary

    return CommandResult(load_session_summary(session.session_store, args[0]))


def _resume(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _require_arg(args, "Usage: /resume <session-id>")
    return CommandResult(session._inject_resume_context(args[0]))


def _fork(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _require_arg(args, "Usage: /fork <session-id>")
    from ..core.interactive import format_fork

    return CommandResult(format_fork(session.session_store, args[0]))


def _rollback(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    if len(args) != 2:
        raise ValueError("Usage: /rollback <session-id> <path>")
    from ..core.interactive import format_rollback_session_file

    return CommandResult(format_rollback_session_file(session.session_store, args[0], args[1]))


def _profiles(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /profiles")
    from ..core.interactive import format_profiles

    return CommandResult(format_profiles())


def _profile(profile_name: str, usage: str) -> CommandHandler:
    def handler(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
        _no_args(args, f"Usage: {usage}")
        return CommandResult(session.switch_profile(profile_name))

    return handler


def _checkpoint(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    return CommandResult(session._handle_checkpoint_command(args))


def _doctor(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /doctor")
    from ..core.interactive import format_doctor

    text, _failures = format_doctor(session.cwd, mcp_manager=getattr(session, "mcp_manager", None))
    return CommandResult(text)


def _mcp(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    if args == ["status"]:
        return CommandResult(session.mcp_status())
    if args == ["list"]:
        return CommandResult(session.mcp_list())
    if args == ["reload"]:
        return CommandResult(session.reload_mcp())
    raise ValueError("Usage: /mcp [status|list|reload]")


def _config(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    if args != ["show"]:
        raise ValueError("Usage: /config show")
    from ..core.interactive import format_config_show

    return CommandResult(format_config_show(session.cwd))


def _observe(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    from ..sessions.observability import (
        export_observability_report,
        format_export_result,
        format_project_observability,
        format_session_observability,
    )

    export = False
    mode = "current"
    if args:
        if args[0] == "export":
            export = True
            if len(args) > 2:
                raise ValueError("Usage: /observe [current|project|export current|export project]")
            mode = args[1] if len(args) == 2 else "current"
        else:
            if len(args) != 1:
                raise ValueError("Usage: /observe [current|project|export current|export project]")
            mode = args[0]
    if mode not in {"current", "project"}:
        raise ValueError("Usage: /observe [current|project|export current|export project]")

    if mode == "current":
        _require_bound(session)
    session_id = session.session.id if mode == "current" else None
    if export:
        result = export_observability_report(
            session.session_store,
            mode=mode,
            session_id=session_id,
        )
        return CommandResult(format_export_result(result))
    if mode == "project":
        return CommandResult(format_project_observability(session.session_store))
    return CommandResult(format_session_observability(session.session_store, session.session.id))


def _require_arg(args: list[str], usage: str) -> None:
    if len(args) != 1:
        raise ValueError(usage)


def _no_args(args: list[str], usage: str) -> None:
    if args:
        raise ValueError(usage)


def _require_bound(session: Any) -> None:
    if not getattr(session, "is_bound", True):
        raise ValueError("No active session yet. Submit a task first.")


def _compact(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    """Handle /compact and its subcommands."""
    if args != ["show"]:
        raise ValueError("Usage: /compact show")
    _require_bound(session)
    summary = _latest_compacted_summary(getattr(session.conversation, "messages", []))
    if summary is None:
        return CommandResult("No compacted summary available yet.")
    return CommandResult(f"Latest compacted summary:\n\n{summary}")


def _latest_compacted_summary(messages: list[dict]) -> str | None:
    for message in reversed(list(messages or [])):
        content = str(message.get("content") or "")
        if not (
            content.startswith("[COMPACTED CONTEXT")
            or content.startswith("[REBUILD_WORKING_CONTEXT]")
        ):
            continue
        _header, _sep, body = content.partition("\n")
        summary = body.strip() or content.strip()
        return summary or None
    return None
