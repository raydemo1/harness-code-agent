from __future__ import annotations

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML, FormattedText

from .state import SessionStatusSnapshot, TranscriptBlock


KIND_STYLES = {
    "user": "ansicyan",
    "assistant": "ansigreen",
    "tool": "ansiyellow",
    "failure": "ansired",
    "approval": "ansimagenta",
    "plan": "ansiblue",
    "profile": "ansiblue",
    "file": "ansiyellow",
    "session": "ansigray",
    "status": "ansigray",
}


def print_welcome(snapshot: SessionStatusSnapshot) -> None:
    print_formatted_text(HTML(
        "<b><ansicyan>Harness Code Agent</ansicyan></b> "
        "<ansigray>inline TUI</ansigray>\n"
        f"<ansigray>session</ansigray> {snapshot.session_id}  "
        f"<ansigray>profile</ansigray> {snapshot.profile}  "
        f"<ansigray>workspace</ansigray> {snapshot.cwd}\n"
        "<ansigray>Use /help for commands. Ctrl-D or /exit closes the session.</ansigray>"
    ))


def print_block(block: TranscriptBlock) -> None:
    style = KIND_STYLES.get(block.kind, "ansiwhite")
    status = f" [{block.status}]" if block.status else ""
    print_formatted_text(HTML(f"\n<{style}>▸ {escape_html(block.title)}{escape_html(status)}</{style}>"))
    if block.body:
        print_formatted_text(block.body)


def print_output(text: str, *, title: str = "output") -> None:
    print_block(TranscriptBlock("status", title, text))


def bottom_toolbar(snapshot: SessionStatusSnapshot, on_context_click=None) -> FormattedText:
    plan = " plan:pending" if snapshot.pending_plan else ""
    tool = f" tool:{snapshot.running_tool}" if snapshot.running_tool else ""
    checkpoint = f" checkpoint:{snapshot.checkpoint}" if snapshot.checkpoint else ""
    compact = ""
    if "compact" in snapshot.status.lower():
        compact = f" compact:{snapshot.status}"
    fragments = [
        ("ansiblack bg:ansigray", f" {snapshot.profile} | turn {snapshot.turn} | {snapshot.permission_mode} "),
        ("ansiblack bg:ansigray", f"| {snapshot.status}{tool}{plan}{checkpoint}{compact} "),
        ("ansiblack bg:ansigray", "| "),
    ]
    fragments.extend(context_indicator_fragments(snapshot, on_context_click=on_context_click))
    fragments.append(("ansiblack bg:ansigray", " "))
    return FormattedText(fragments)


def context_indicator_fragments(snapshot: SessionStatusSnapshot, on_context_click=None) -> list[tuple]:
    percent = _context_percent(snapshot)
    observe_percent = _threshold_percent(snapshot, snapshot.context_observe_threshold, 60)
    prepare_percent = _threshold_percent(snapshot, snapshot.context_prepare_threshold, 68)
    allow_percent = _threshold_percent(snapshot, snapshot.context_allow_threshold, 75)
    force_percent = _threshold_percent(snapshot, snapshot.context_force_threshold, 82)
    band_style = _context_band_style(percent, prepare_percent=prepare_percent, force_percent=force_percent)
    hint = _context_hint(snapshot, percent) if snapshot.context_hint else "hover ctx for usage; click to compact"

    def mouse_handler(mouse_event):
        event_type = str(getattr(mouse_event, "event_type", ""))
        snapshot.context_hint = True
        if "MOUSE_UP" in event_type and on_context_click is not None:
            on_context_click()
        return None

    clickable = mouse_handler if on_context_click is not None else None
    return [
        ("ansiwhite bg:ansigray", "ctx ", clickable),
        ("ansigreen bg:ansigray", f"○{observe_percent} ", clickable),
        ("ansiyellow bg:ansigray", f"○{prepare_percent} ", clickable),
        ("ansiyellow bg:ansigray", f"○{allow_percent} ", clickable),
        ("ansired bg:ansigray", f"○{force_percent} ", clickable),
        (f"{band_style} bg:ansigray", f"{percent}% ", clickable),
        ("ansiblack bg:ansigray", f"{hint}", clickable),
    ]


def _context_percent(snapshot: SessionStatusSnapshot) -> int:
    if snapshot.context_window_tokens <= 0:
        return 0
    return min(999, int(round(snapshot.context_tokens * 100 / snapshot.context_window_tokens)))


def _threshold_percent(snapshot: SessionStatusSnapshot, threshold: int, fallback: int) -> int:
    if snapshot.context_window_tokens <= 0 or threshold <= 0:
        return fallback
    return min(999, int(round(threshold * 100 / snapshot.context_window_tokens)))


def _context_band_style(percent: int, *, prepare_percent: int, force_percent: int) -> str:
    if percent >= force_percent:
        return "ansired"
    if percent >= prepare_percent:
        return "ansiyellow"
    return "ansigreen"


def _context_hint(snapshot: SessionStatusSnapshot, percent: int) -> str:
    tokens = f"{snapshot.context_tokens:,}/{snapshot.context_window_tokens:,}".replace(",", "_")
    return f"context {percent}% ({tokens} tokens); click ○ to compact"


def prompt_message(snapshot: SessionStatusSnapshot) -> HTML:
    if snapshot.pending_plan:
        return HTML(
            "<ansiblue>计划已就绪</ansiblue> "
            "<ansigreen>[执行计划]</ansigreen> "
            "<ansigray>[修改计划: 输入修改理由或补充要求]</ansigray>\n"
            "<ansicyan>hca</ansicyan> › "
        )
    return HTML("<ansicyan>hca</ansicyan> › ")


def escape_html(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
