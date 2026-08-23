from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    agent_command: bool = False

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
            return CommandResult(f"错误：{exc}")
        if not parts:
            return CommandResult()
        command, args = parts[0], parts[1:]
        spec = self._by_name.get(command)
        if spec is None:
            return CommandResult(f"未知的斜杠命令：{command}")
        try:
            return spec.handler(session, args, self)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            return CommandResult(f"错误：{exc}")

    def command_names(self) -> list[str]:
        return [spec.name for spec in self.specs]

    def is_agent_command(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError:
            return False
        if not parts:
            return False
        spec = self._by_name.get(parts[0])
        return bool(spec and spec.agent_command)

    def candidates(self) -> list[CommandSpec]:
        return list(self.specs)

    def format_help(self) -> str:
        lines = ["VeriForge 命令："]
        current_group = ""
        for spec in self.specs:
            if spec.group != current_group:
                current_group = spec.group
                lines.append("")
                lines.append(f"{current_group}:")
            lines.append(f"  {spec.usage:45s} {spec.description}")
        return "\n".join(lines)


def default_command_registry(skill_registry=None) -> SlashCommandRegistry:
    specs = [
        CommandSpec("/help", "常用", "/help", "按工作流查看命令。", _help),
        CommandSpec("/exit", "常用", "/exit", "关闭当前 TUI 会话。", _exit, aliases=("/quit",)),
        CommandSpec("/profiles", "配置", "/profiles", "列出可用配置。", _profiles),
        CommandSpec("/general", "配置", "/general", "切换到通用配置。", _profile("general", "/general")),
        CommandSpec("/code", "配置", "/code", "切换到编码代理配置。", _profile("coding-agent", "/code")),
        CommandSpec("/plan", "配置", "/plan", "切换到受约束的规划配置。", _profile("plan", "/plan")),
        CommandSpec("/app", "配置", "/app", "切换到应用构建配置。", _profile("app-builder", "/app")),
        CommandSpec("/review", "配置", "/review", "切换到只读审查配置。", _profile("review", "/review")),
        CommandSpec("/sessions", "会话", "/sessions", "列出本地 Harness 会话。", _sessions),
        CommandSpec("/session", "会话", "/session <session-id>", "查看可读的会话摘要。", _session),
        CommandSpec("/fork", "会话", "/fork <session-id>", "创建保留血缘的分支记录。", _fork),
        CommandSpec("/rollback", "会话", "/rollback <session-id> <path>", "从最近的会话快照恢复一个文件。", _rollback),
        CommandSpec("/checkpoint", "工作流", "/checkpoint [auto on|auto off|every turn|every <N> turns|status]", "创建或配置检查点提交。", _checkpoint),
        CommandSpec("/mcp", "诊断", "/mcp [status|list|reload]", "查看或重新加载 MCP 服务和工具。", _mcp),
        CommandSpec("/doctor", "诊断", "/doctor", "检查 API、工作区、Git 和 Shell 配置。", _doctor),
        CommandSpec("/config", "诊断", "/config show", "查看生效的 Harness 配置。", _config),
        CommandSpec("/observe", "诊断", "/observe [current|project|export current|export project]", "查看或导出可观测性指标。", _observe),
        CommandSpec("/compact", "工作流", "/compact show", "查看最近一次压缩摘要。", _compact),
    ]
    if skill_registry is None:
        from ..skills import SkillRegistry

        skill_registry = SkillRegistry()
    occupied = {
        name
        for spec in specs
        for name in spec.names()
    }
    for item in getattr(skill_registry, "user_commands", []):
        skill_name = str(item.get("name", "")).strip()
        if not skill_name:
            continue
        command_name = f"/{skill_name}"
        if command_name in occupied:
            raise ValueError(
                f"User skill command {command_name} conflicts with built-in command"
            )
        hint = str(item.get("argument_hint", "")).strip()
        description = str(item.get("description", "")).strip()
        usage = f"{command_name} {hint}".rstrip()
        specs.append(
            CommandSpec(
                command_name,
                "Skills",
                usage,
                description,
                _user_skill(command_name),
                agent_command=True,
            )
        )
        occupied.add(command_name)
    return SlashCommandRegistry(specs)


def _user_skill(command_name: str) -> CommandHandler:
    def handler(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
        line = " ".join([command_name, *args])
        result = session.submit_skill_command(line)
        return CommandResult(result.text)

    return handler


def _help(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /help")
    return CommandResult(registry.format_help())


def _exit(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /exit")
    return CommandResult(should_continue=False)


def _sessions(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /sessions")
    from ..core.formatters import format_sessions

    return CommandResult(format_sessions(session.session_store))


def _session(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _require_arg(args, "Usage: /session <session-id>")
    from ..sessions.summary import load_session_summary

    return CommandResult(load_session_summary(session.session_store, args[0]))


def _fork(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _require_arg(args, "Usage: /fork <session-id>")
    from ..core.formatters import format_fork

    return CommandResult(format_fork(session.session_store, args[0]))


def _rollback(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    if len(args) != 2:
        raise ValueError("Usage: /rollback <session-id> <path>")
    from ..core.formatters import format_rollback_session_file

    return CommandResult(format_rollback_session_file(session.session_store, args[0], args[1]))


def _profiles(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    _no_args(args, "Usage: /profiles")
    from ..core.formatters import format_profiles

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
    from ..core.formatters import format_doctor

    text, _failures = format_doctor(session.cwd, mcp_manager=getattr(session, "mcp_manager", None))
    return CommandResult(text)


def _mcp(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    if args == ["status"]:
        return CommandResult(session.mcp_status())
    if args == ["list"]:
        return CommandResult(session.mcp_list())
    if args == ["reload"]:
        return CommandResult(session.reload_mcp())
        raise ValueError("用法：/mcp [status|list|reload]")


def _config(session: Any, args: list[str], registry: SlashCommandRegistry) -> CommandResult:
    if args != ["show"]:
        raise ValueError("用法：/config show")
    from ..core.formatters import format_config_show

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
                raise ValueError("用法：/observe [current|project|export current|export project]")
            mode = args[1] if len(args) == 2 else "current"
        else:
            if len(args) != 1:
                raise ValueError("用法：/observe [current|project|export current|export project]")
            mode = args[0]
    if mode not in {"current", "project"}:
        raise ValueError("用法：/observe [current|project|export current|export project]")

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
    summary = _latest_compacted_summary_from_disk(session)
    if summary is None:
        summary = _latest_compacted_summary(getattr(session.conversation, "messages", []))
    if summary is None:
        return CommandResult("目前还没有可用的压缩摘要。")
    return CommandResult(f"最近的压缩摘要：\n\n{summary}")


def _latest_compacted_summary_from_disk(session: Any) -> str | None:
    session_obj = getattr(session, "session", None)
    compacted_dir = getattr(session_obj, "compacted_dir", None)
    if not isinstance(compacted_dir, (str, Path)):
        return None
    path = Path(compacted_dir) / "latest.md"
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


def _latest_compacted_summary(messages: list[dict]) -> str | None:
    for message in reversed(list(messages or [])):
        content = str(message.get("content") or "")
        if not (
            content.startswith(("[COMPACTED CONTEXT", "[HANDOFF RESET]"))
        ):
            continue
        _header, _sep, body = content.partition("\n")
        summary = body.strip() or content.strip()
        return summary or None
    return None
