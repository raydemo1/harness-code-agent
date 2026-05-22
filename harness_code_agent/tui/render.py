from __future__ import annotations

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML

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


def bottom_toolbar(snapshot: SessionStatusSnapshot) -> HTML:
    plan = " plan:pending" if snapshot.pending_plan else ""
    tool = f" tool:{snapshot.running_tool}" if snapshot.running_tool else ""
    checkpoint = f" checkpoint:{snapshot.checkpoint}" if snapshot.checkpoint else ""
    return HTML(
        '<style fg="ansiblack" bg="ansigray">'
        f" {escape_html(snapshot.profile)} | turn {snapshot.turn} | {escape_html(snapshot.permission_mode)} "
        f"| {escape_html(snapshot.status)}{escape_html(tool)}{escape_html(plan)}{escape_html(checkpoint)} "
        "</style>"
    )


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
