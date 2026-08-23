"""Inline approval and question panels for the TUI."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from ..runtime.approvals import ApprovalRequest
    from ..runtime.questions import QuestionRequest

log = logging.getLogger("harness.tui.screens")


# ── Messages ──────────────────────────────────────────────────────────────

class ApprovalResult(Message, bubble=True):
    """Message posted when user makes an approval decision."""
    def __init__(self, approved: bool) -> None:
        super().__init__()
        self.approved = approved


class QuestionResult(Message, bubble=True):
    """Message posted when user answers a question."""
    def __init__(self, payload: dict | None) -> None:
        super().__init__()
        self.payload = payload


class ProfileSelectionResult(Message, bubble=True):
    """A profile was selected in the profile picker."""

    def __init__(self, profile_name: str | None) -> None:
        super().__init__()
        self.profile_name = profile_name


class CheckpointActionResult(Message, bubble=True):
    """A checkpoint action was selected."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action


class McpActionResult(Message, bubble=True):
    """An MCP management action was selected."""

    def __init__(self, action: str, server_name: str | None = None) -> None:
        super().__init__()
        self.action = action
        self.server_name = server_name


class ResumeSessionScreen(ModalScreen[dict | None]):
    """Searchable in-app picker for local history sessions.

    The picker deliberately owns the human-facing flow. Session identifiers are
    kept only as option metadata so users choose a readable session entry
    instead of having to remember or type an internal id.
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "close", "关闭", show=False, priority=True),
        Binding("ctrl+c", "close", "关闭", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ResumeSessionScreen {
        align: center middle;
    }

    #resume-panel {
        width: 84;
        height: 68%;
        min-height: 12;
        border: solid $border-blurred;
        background: $surface;
        padding: 1 2;
    }

    #resume-search {
        height: 3;
        margin-bottom: 1;
    }

    #resume-options {
        height: 1fr;
        border: solid $border-blurred;
        background: $background;
    }

    #resume-status {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        load_sessions: Callable[[], list[dict[str, Any]]],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._load_sessions_fn = load_sessions
        self._sessions: list[dict[str, Any]] = []
        self._filtered_sessions: list[dict[str, Any]] = []
        self._spinner_index = 0
        self._loading = True

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-panel"):
            yield Input(placeholder="⌕  搜索会话…", id="resume-search")
            yield OptionList(id="resume-options")
            yield Static("⠋", id="resume-status")

    def on_mount(self) -> None:
        self.set_interval(0.12, self._advance_spinner)
        self.query_one("#resume-search", Input).focus()
        self._load_sessions()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _load_sessions(self) -> None:
        try:
            sessions = list(self._load_sessions_fn())
        except Exception as exc:  # noqa: BLE001 - picker must surface loader failures
            self.post_message(ResumeSessionLoadFailed(f"{type(exc).__name__}: {exc}"))
            return
        self.post_message(ResumeSessionListLoaded(sessions))

    def _advance_spinner(self) -> None:
        if not self._loading:
            return
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spinner_index = (self._spinner_index + 1) % len(frames)
        try:
            self.query_one("#resume-status", Static).update(
                frames[self._spinner_index]
            )
        except NoMatches:
            return

    def on_resume_session_list_loaded(self, message: ResumeSessionListLoaded) -> None:
        self._loading = False
        self._sessions = message.sessions
        self._apply_filter("")
        self.query_one("#resume-options", OptionList).focus()
        status = "暂无历史会话" if not self._sessions else ""
        self.query_one("#resume-status", Static).update(status)
        self.query_one("#resume-status", Static).display = not bool(self._sessions)

    def on_resume_session_load_failed(self, message: ResumeSessionLoadFailed) -> None:
        self._loading = False
        self.query_one("#resume-status", Static).update("⚠")
        self.query_one("#resume-options", OptionList).clear_options()

    @on(Input.Changed, "#resume-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(Input.Submitted, "#resume-search")
    def _on_search_submitted(self) -> None:
        options = self.query_one("#resume-options", OptionList)
        if options.option_count:
            options.focus()
            options.action_select()

    @on(OptionList.OptionSelected, "#resume-options")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        if not option_id or not option_id.startswith("session:"):
            return
        session_id = option_id.removeprefix("session:")
        selected = next(
            (item for item in self._filtered_sessions if str(item.get("id")) == session_id),
            None,
        )
        if selected is not None:
            self.dismiss(selected)

    def action_close(self) -> None:
        self.dismiss(None)

    def _apply_filter(self, query: str) -> None:
        normalized = " ".join(str(query or "").lower().split())
        if not normalized:
            self._filtered_sessions = list(self._sessions)
        else:
            self._filtered_sessions = [
                item
                for item in self._sessions
                if normalized in str(item.get("search_text", "")).lower()
            ]
        options = self.query_one("#resume-options", OptionList)
        options.clear_options()
        for item in self._filtered_sessions:
            options.add_option(
                Option(
                    _session_option_prompt(item),
                    id=f"session:{item.get('id', '')}",
                )
            )


def _session_option_prompt(item: dict[str, Any]) -> Text:
    """Render a quiet title + relative age row, keeping ids out of the UI."""
    title = str(item.get("label") or "未命名")
    age = str(item.get("age") or "")
    prompt = Text(title, style="#f5f5f5")
    if age:
        prompt.append("    ")
        prompt.append(age, style="#9a9a9a")
    return prompt


class ResumeSessionListLoaded(Message):
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.sessions = sessions


class ResumeSessionLoadFailed(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


# ── Workflow panels ────────────────────────────────────────────────────────

class ProfileSelectorScreen(ModalScreen[None]):
    """Clickable profile selector; one conversation is shared by all modes."""

    DEFAULT_CSS = """
    ProfileSelectorScreen { align: center middle; }
    #profile-panel { width: 66; height: auto; max-height: 70%; border: solid $border-blurred; background: $surface; padding: 1 2; }
    #profile-title { height: 2; color: $accent; }
    #profile-options { height: auto; max-height: 16; border: none; background: $background; }
    #profile-footer { height: 1; margin-top: 1; color: $text-muted; }
    """

    _PROFILES = (
        (None, "自动路由", "根据当前任务选择工作模式"),
        ("general", "通用", "问答、分析与轻量任务"),
        ("coding-agent", "编码", "修改代码、运行测试与验证"),
        ("plan", "规划", "拆解方案与实施步骤"),
        ("app-builder", "应用构建", "端到端构建应用界面"),
        ("review", "审查", "只读检查与风险分析"),
    )

    def __init__(self, session: Any, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="profile-panel"):
            yield Static("工作模式", id="profile-title")
            yield OptionList(id="profile-options")
            yield Static("选择后会固定模式；再次选择自动路由即可恢复自动判断。", id="profile-footer")

    def on_mount(self) -> None:
        options = self.query_one("#profile-options", OptionList)
        current = getattr(self._session, "display_profile", "")
        if getattr(self._session, "display_routing_mode", "") == "auto":
            current = "auto"
        for profile_name, label, description in self._PROFILES:
            marker = "●" if (profile_name or "auto") == current else "○"
            options.add_option(
                Option(f"{marker}  {label}  ·  {description}", id=f"profile:{profile_name or 'auto'}")
            )
        options.focus()

    @on(OptionList.OptionSelected, "#profile-options")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if not event.option_id or not event.option_id.startswith("profile:"):
            return
        value = event.option_id.removeprefix("profile:")
        self.post_message(ProfileSelectionResult(None if value == "auto" else value))
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class CheckpointScreen(ModalScreen[None]):
    """Small checkpoint control panel."""

    DEFAULT_CSS = """
    CheckpointScreen { align: center middle; }
    #checkpoint-panel { width: 66; height: auto; border: solid $border-blurred; background: $surface; padding: 1 2; }
    #checkpoint-title { height: 2; color: $accent; }
    #checkpoint-options { height: auto; border: none; background: $background; }
    #checkpoint-status { height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, session: Any, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoint-panel"):
            yield Static("检查点", id="checkpoint-title")
            yield OptionList(
                Option("立即创建检查点", id="create"),
                Option("自动检查点：开启", id="auto_on"),
                Option("自动检查点：关闭", id="auto_off"),
                Option("每轮创建", id="every_turn"),
                id="checkpoint-options",
            )
            yield Static("", id="checkpoint-status")

    def on_mount(self) -> None:
        checkpoint = getattr(self._session, "checkpoint", None)
        if checkpoint is not None:
            status = f"当前：自动{'开启' if checkpoint.auto else '关闭'} · 每 {checkpoint.every_turns} 轮"
            self.query_one("#checkpoint-status", Static).update(status)
        self.query_one("#checkpoint-options", OptionList).focus()

    @on(OptionList.OptionSelected, "#checkpoint-options")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.post_message(CheckpointActionResult(event.option_id))
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class McpManagerScreen(ModalScreen[None]):
    """Runtime MCP dashboard with reconnect/reload and config toggles."""

    DEFAULT_CSS = """
    McpManagerScreen { align: center middle; }
    #mcp-panel { width: 88; height: 76%; border: solid $border-blurred; background: $surface; padding: 1 2; }
    #mcp-title { height: 2; color: $accent; }
    #mcp-options { height: 1fr; border: none; background: $background; }
    #mcp-status { height: 2; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, session: Any, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-panel"):
            yield Static("MCP 管理", id="mcp-title")
            yield OptionList(id="mcp-options")
            yield Static("", id="mcp-status")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        options = self.query_one("#mcp-options", OptionList)
        options.clear_options()
        options.add_option(Option("重新加载全部 MCP 服务", id="reload"))
        manager = getattr(self._session, "mcp_manager", None)
        statuses = getattr(manager, "statuses", {}) if manager is not None else {}
        names = set(getattr(manager, "configured_server_names", list)())
        names.update(name for name in statuses if name != "config")
        for name in sorted(names):
            status = statuses.get(name)
            if status is None:
                state = "已停用"
            else:
                state = "已连接" if status.state == "connected" else "连接失败"
            options.add_option(Option(f"重新连接：{name}  ·  {state}", id=f"reconnect:{name}"))
            options.add_option(Option(f"切换启用：{name}", id=f"toggle:{name}"))
        if manager is not None:
            options.add_option(Option("打开 mcp.json", id="open_config"))
            self.query_one("#mcp-status", Static).update(_mcp_summary(manager))
        else:
            self.query_one("#mcp-status", Static).update("尚未加载 MCP 配置")
        options.focus()

    @on(OptionList.OptionSelected, "#mcp-options")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if not event.option_id:
            return
        if ":" in event.option_id:
            action, name = event.option_id.split(":", 1)
        else:
            action, name = event.option_id, None
        self.post_message(McpActionResult(action, name))
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


def _mcp_summary(manager: Any) -> str:
    statuses = list(getattr(manager, "statuses", {}).values())
    connected = sum(item.state == "connected" for item in statuses)
    tools = len(getattr(manager, "tool_bindings", []))
    return f"已连接 {connected} 个服务 · 已注册 {tools} 个工具"


# ── ApprovalPanel ───────────────────────────────────────────────────────────

_APPROVE = 0
_PERSIST = 1
_DENY = 2

_APPROVAL_LABELS = ["仅本次允许", "信任此前缀", "拒绝"]
_RISK_LABELS = {
    "shell_safe": "Shell 安全",
    "shell_risky": "Shell 有风险",
    "shell_blocked": "Shell 已拦截",
}
_ARG_LABELS = {
    "path": "路径",
    "file_path": "文件路径",
    "command": "命令",
    "url": "地址",
    "task": "任务",
    "query": "查询",
}


class ApprovalPanel(Vertical):
    """Inline approval panel that replaces the input area.

    Double-key-press behavior: first press selects, second press submits.
    Posts ApprovalResult message on decision.
    """

    can_focus = True

    DEFAULT_CSS = """
    ApprovalPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, request: ApprovalRequest, **kwargs):
        super().__init__(**kwargs)
        self._request = request
        from .approval import _persistent_prefix_for_request
        self._persist_available = _persistent_prefix_for_request(request) is not None
        self._selected_index = _APPROVE
        self._armed_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._format_body(), classes="body")
        yield Static(self._format_choices(), id="approval-choices")

    def _format_body(self) -> str:
        req = self._request
        lines = [
            "[bold]⚠ 需要确认[/]",
            f"工具：{req.tool_name}    风险：{_RISK_LABELS.get(req.risk, req.risk)}",
        ]
        if req.reason:
            lines.append(f"原因：{_localize_approval_reason(req.reason)}")
        if req.tool_name == "run_bash":
            cmd = req.args.get("command", "")
            lines.append(f"命令：$ {cmd}")
        else:
            priority = ("path", "file_path", "command", "url", "task", "query")
            keys = sorted(
                (req.args or {}).keys(),
                key=lambda key: (priority.index(key) if key in priority else len(priority), key),
            )
            for key in keys:
                val_str = str((req.args or {}).get(key, ""))
                if len(val_str) > 200:
                    val_str = val_str[:197] + "..."
                lines.append(f"  {_ARG_LABELS.get(key, key)}：{val_str}")
        lines.append("[dim]←→ 或 1-3 选择 · 回车确认 · Esc 拒绝[/]")
        return "\n".join(lines)

    def _format_choices(self) -> Text:
        parts = []
        for i, label in enumerate(_APPROVAL_LABELS):
            if i == _PERSIST and not self._persist_available:
                label = "无法信任"
            marker = "▶" if i == self._selected_index else " "
            parts.append((f"{marker} [{i + 1}] {label}", i == self._selected_index))
        text = Text()
        for i, (part, selected) in enumerate(parts):
            if i:
                text.append("   ", style="dim")
            if selected:
                text.append(part, style="bold")
            elif i == _PERSIST and not self._persist_available:
                text.append(part, style="dim")
            elif part.endswith("拒绝"):
                text.append(part, style="red")
            else:
                text.append(part)
        return text

    def _refresh_choices(self) -> None:
        try:
            self.query_one("#approval-choices", Static).update(self._format_choices())
        except NoMatches:
            log.debug("approval-choices not found")
        except Exception:
            log.warning("Error refreshing approval choices", exc_info=True)

    def on_key(self, event) -> None:
        if self.handle_key(event.key):
            event.prevent_default()
            event.stop()

    def handle_key(self, key: str) -> bool:
        """Handle a key press routed from either this panel or the app."""

        # Number keys 1-3: double-press to submit
        if key.isdigit() and 1 <= int(key) <= 3:
            num = int(key)
            target = num - 1
            if target == _PERSIST and not self._persist_available:
                return True
            if self._selected_index == target and self._armed_key == key:
                self._submit()
                return True
            self._selected_index = target
            self._armed_key = key
            self._refresh_choices()
            return True

        # Arrow keys
        if key == "left":
            choices = [_APPROVE, _PERSIST, _DENY] if self._persist_available else [_APPROVE, _DENY]
            current = choices.index(self._selected_index)
            self._selected_index = choices[(current - 1) % len(choices)]
            self._armed_key = None
            self._refresh_choices()
            return True
        elif key == "right":
            choices = [_APPROVE, _PERSIST, _DENY] if self._persist_available else [_APPROVE, _DENY]
            current = choices.index(self._selected_index)
            self._selected_index = choices[(current + 1) % len(choices)]
            self._armed_key = None
            self._refresh_choices()
            return True
        elif key == "enter":
            self._submit()
            return True
        elif key == "escape":
            self._deny()
            return True
        return False

    def _submit(self) -> None:
        if self._selected_index == _PERSIST:
            from .approval import TuiApprovalProvider, _persistent_prefix_for_request
            provider = TuiApprovalProvider(project_root=self.app.cwd)
            persistent_prefix = _persistent_prefix_for_request(self._request)
            if provider.allowlist is not None and persistent_prefix:
                provider.allowlist.add_prefix_rule(
                    persistent_prefix,
                    command=str(self._request.args.get("command", "")),
                )
        approved = self._selected_index in (_APPROVE, _PERSIST)
        self.post_message(ApprovalResult(approved))

    def _deny(self) -> None:
        self.post_message(ApprovalResult(False))


# ── QuestionPanel ───────────────────────────────────────────────────────────

class QuestionPanel(Vertical):
    """Inline question panel that replaces the input area.

    Double-key-press behavior: first press selects, second press submits.
    """

    can_focus = True

    DEFAULT_CSS = """
    QuestionPanel {
        height: auto;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, request: QuestionRequest, **kwargs):
        super().__init__(**kwargs)
        self._request = request
        self._selected_index = 0
        self._other_text = ""
        self._armed_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._format_body(), id="q-body", classes="body")
        yield Static(self._format_choices(), id="q-choices")
        yield Input(placeholder="其他说明…", id="q-other-input")

    def on_mount(self) -> None:
        other = self.query_one("#q-other-input", Input)
        other.display = False

    def _format_body(self) -> str:
        req = self._request
        lines = [f"[bold]? {req.question}[/]"]
        for i, opt in enumerate(req.options, 1):
            desc = f" — {opt.description}" if opt.description else ""
            lines.append(f"  [{i}] {opt.label}{desc}")
        return "\n".join(lines)

    def _format_choices(self) -> str:
        parts = []
        for i, opt in enumerate(self._request.options, 1):
            marker = "▶" if i - 1 == self._selected_index else " "
            parts.append(f"{marker} [{i}] {opt.label}")
        return "   ".join(parts)

    def _refresh_choices(self) -> None:
        try:
            self.query_one("#q-choices", Static).update(self._format_choices())
        except NoMatches:
            log.debug("q-choices not found")
        except Exception:
            log.warning("Error refreshing question choices", exc_info=True)

    def _update_other_visibility(self) -> None:
        opt = self._request.options[self._selected_index]
        other = self.query_one("#q-other-input", Input)
        if opt.is_other:
            other.display = True
        else:
            other.display = False

    def on_key(self, event) -> None:
        if self.handle_key(event.key):
            event.prevent_default()
            event.stop()

    def handle_key(self, key: str) -> bool:
        """Handle a key press routed from either this panel or the app."""

        # Number keys 1-9: double-press to submit
        if key.isdigit() and 1 <= int(key) <= len(self._request.options):
            num = int(key)
            target = num - 1
            # If on "Other" and text is being typed, treat as text input
            if self._request.options[self._selected_index].is_other and (
                self._other_text or self._armed_key is None or self._armed_key != key
            ):
                self._other_text += key
                self._armed_key = None
                return True
            # Double-press submits
            if self._selected_index == target and self._armed_key == key:
                self._submit()
                return True
            self._selected_index = target
            self._armed_key = key
            self._refresh_choices()
            self._update_other_visibility()
            return True

        if key == "up":
            self._selected_index = (self._selected_index - 1) % len(self._request.options)
            self._armed_key = None
            self._refresh_choices()
            self._update_other_visibility()
            return True
        elif key == "down":
            self._selected_index = (self._selected_index + 1) % len(self._request.options)
            self._armed_key = None
            self._refresh_choices()
            self._update_other_visibility()
            return True
        elif key == "enter":
            self._submit()
            return True
        elif key == "escape":
            self._cancel()
            return True
        return False

    def _submit(self) -> None:
        from ..runtime.questions import question_result_from_option
        opt = self._request.options[self._selected_index]
        custom_text = ""
        if opt.is_other:
            other_input = self.query_one("#q-other-input", Input)
            custom_text = other_input.value.strip()
        result = question_result_from_option(
            self._request,
            self._selected_index,
            custom_text=custom_text,
            metadata={"ui": "tui"},
        )
        self.post_message(QuestionResult(result.to_dict()))

    def _cancel(self) -> None:
        self.post_message(QuestionResult(None))


# ── ObservabilityScreen ─────────────────────────────────────────────────────

class ObservabilityScreen(ModalScreen[None]):
    """Temporary observability dashboard opened by keyboard shortcut."""

    BINDINGS: ClassVar[list] = [
        Binding("escape", "close", "关闭", show=False, priority=True),
        Binding("tab", "toggle_mode", "切换范围", show=False, priority=True),
        Binding("r", "refresh", "刷新", show=False, priority=True),
        Binding("e", "export", "导出", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ObservabilityScreen {
        align: center middle;
    }

    #observability-panel {
        width: 96;
        height: 80%;
        border: solid $border-blurred;
        background: $surface;
        padding: 1 2;
    }

    #observability-title {
        height: 1;
        color: $accent;
    }

    #observability-body {
        height: 1fr;
        overflow-y: auto;
    }

    #observability-footer {
        height: auto;
        color: #9a9a9a;
    }
    """

    def __init__(self, session: Any, *, initial_mode: str = "current", **kwargs):
        super().__init__(**kwargs)
        self._session = session
        self._mode = "project" if initial_mode == "project" else "current"
        self._message = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="observability-panel"):
            yield Static("", id="observability-title")
            yield Static("", id="observability-body")
            yield Static("", id="observability-footer")

    def on_mount(self) -> None:
        self._refresh()

    def action_close(self) -> None:
        self.dismiss()

    def action_toggle_mode(self) -> None:
        self._mode = "project" if self._mode == "current" else "current"
        self._message = ""
        self._refresh()

    def action_refresh(self) -> None:
        self._message = "已刷新"
        self._refresh()

    def action_export(self) -> None:
        from ..sessions.observability import (
            export_observability_report,
            format_export_result,
        )

        session_id = self._current_session_id() if self._mode == "current" else None
        if self._mode == "current" and not session_id:
            self._message = "当前还没有会话，请先提交任务。"
            self._refresh()
            return
        result = export_observability_report(
            self._session.session_store,
            mode=self._mode,
            session_id=session_id,
        )
        self._message = format_export_result(result)
        self._refresh()

    def _refresh(self) -> None:
        from ..sessions.observability import (
            format_project_observability,
            format_session_observability,
        )

        if self._mode == "project":
            title = "可观测性 · 项目概览"
            body = format_project_observability(self._session.session_store)
        else:
            title = "可观测性 · 当前会话"
            session_id = self._current_session_id()
            body = (
                format_session_observability(self._session.session_store, session_id)
                if session_id
                else self._current_session_body()
            )
        body = _localize_observability_text(body)
        footer = "Tab：切换范围  R：刷新  E：导出  Esc：关闭"
        if self._message:
            footer += f"\n{self._message}"
        self.query_one("#observability-title", Static).update(title)
        self.query_one("#observability-body", Static).update(body)
        self.query_one("#observability-footer", Static).update(footer)

    def _current_session_id(self) -> str | None:
        session = getattr(self._session, "session", None)
        session_id = getattr(session, "id", None)
        return str(session_id) if session_id else None

    def _current_session_body(self) -> str:
        return "当前还没有会话，请先提交任务。"


def _localize_observability_text(text: str) -> str:
    """Translate the observability formatter for the Chinese TUI surface."""
    replacements = {
        "Observability dashboard": "可观测性面板",
        "Project observability": "项目可观测性",
        "session:": "会话：",
        "profile:": "配置：",
        "model:": "模型：",
        "status:": "状态：",
        "created_at:": "创建时间：",
        "tokens:": "令牌：",
        "tools:": "工具：",
        "performance:": "性能：",
        "audit:": "审计：",
        "tool breakdown:": "工具明细：",
        "recent audit events:": "最近审计事件：",
        "top token sessions:": "令牌消耗最多的会话：",
        "top failure sessions:": "失败最多的会话：",
        "low cache sessions:": "缓存命中率较低的会话：",
        "sessions:": "会话数：",
        "calls=": "调用=",
        "results=": "结果=",
        "success=": "成功=",
        "failed=": "失败=",
        "unknown=": "未知=",
        "pending=": "待处理=",
        "cache hit ratio:": "缓存命中率：",
        "success rate:": "成功率：",
        "缓存命中率： ": "缓存命中率：",
        "成功率： ": "成功率：",
        "llm_调用=": "模型调用=",
        "llm_response=": "模型响应=",
        "ttft=": "首 token=",
        "turn=": "回合=",
        "pending: ": "待处理：",
    }
    localized = str(text or "")
    for source, target in replacements.items():
        localized = localized.replace(source, target)
    return localized


def _localize_approval_reason(reason: str) -> str:
    replacements = {
        "workspace-write mode requires user approval for non-whitelisted commands and tools": "工作区可写模式下，未在白名单中的命令和工具需要用户确认",
        "workspace-write mode requires user approval": "工作区可写模式需要用户确认",
        "permission policy denied": "权限策略拒绝了此次执行",
    }
    localized = str(reason or "")
    for source, target in replacements.items():
        localized = localized.replace(source, target)
    return localized
