from __future__ import annotations

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML, FormattedText, StyleAndTextTuples

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

# Left border colors per message kind
_KIND_BORDER_COLORS = {
    "user": "#3874cb",
    "assistant": "#4caf50",
    "tool": "#d79921",
    "thought": "#b48ead",
    "failure": "#bf616a",
    "approval": "#b16286",
    "plan": "#3874cb",
    "profile": "#3874cb",
    "file": "#d79921",
    "session": "#555555",
    "status": "#555555",
}

# Card background colors for special blocks
_KIND_CARD_BG = {
    "tool": "#1e1e2e",
    "thought": "#1a1026",
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
        ("bg:#2d2d3d #cccccc", f" {snapshot.profile} | turn {snapshot.turn} | {snapshot.permission_mode} "),
        ("bg:#2d2d3d #cccccc", f"| {snapshot.status}{tool}{plan}{checkpoint}{compact} "),
        ("bg:#2d2d3d #cccccc", "| "),
    ]
    fragments.extend(context_indicator_fragments(snapshot, on_context_click=on_context_click))
    fragments.append(("bg:#2d2d3d", " "))
    return FormattedText(fragments)


def context_indicator_fragments(snapshot: SessionStatusSnapshot, on_context_click=None) -> list[tuple]:
    percent = _context_percent(snapshot)
    prepare_percent = _threshold_percent(snapshot, snapshot.context_prepare_threshold, 68)
    force_percent = _threshold_percent(snapshot, snapshot.context_force_threshold, 82)
    bar_width = 20
    filled = int(bar_width * min(percent, 100) / 100)
    bar = "▓" * filled + "░" * (bar_width - filled)
    band_style = _context_color(percent, prepare_percent=prepare_percent, force_percent=force_percent)
    hint = _context_hint(snapshot, percent) if snapshot.context_hint else "click ctx to compact"

    def mouse_handler(mouse_event):
        event_type = str(getattr(mouse_event, "event_type", ""))
        snapshot.context_hint = True
        if "MOUSE_UP" in event_type and on_context_click is not None:
            on_context_click()
        return None

    clickable = mouse_handler if on_context_click is not None else None
    return [
        ("bg:#2d2d3d #aaaaaa", "ctx ", clickable),
        (f"bg:#2d2d3d {band_style}", bar, clickable),
        (f"bg:#2d2d3d {band_style} bold", f" {percent}% ", clickable),
        ("bg:#2d2d3d #888888", f"{_context_token_label(snapshot)}  {hint}", clickable),
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


def _context_color(percent: int, *, prepare_percent: int, force_percent: int) -> str:
    if percent >= force_percent:
        return "#bf616a"
    if percent >= prepare_percent:
        return "#ebcb8b"
    return "#a3be8c"


def _context_token_label(snapshot: SessionStatusSnapshot) -> str:
    return f"{snapshot.context_tokens // 1000}K/{snapshot.context_window_tokens // 1000}K"


def _context_hint(snapshot: SessionStatusSnapshot, percent: int) -> str:
    tokens = f"{snapshot.context_tokens:,}/{snapshot.context_window_tokens:,}".replace(",", "_")
    return f"context {percent}% ({tokens} tokens); click ctx to compact"


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


# ---------------------------------------------------------------------------
# New async TUI rendering functions (Phase 1+)
# ---------------------------------------------------------------------------

def context_bar_fragments(
    snapshot: SessionStatusSnapshot,
    on_context_click=None,
) -> StyleAndTextTuples:
    """Render context bar as progress bar + percentage + token count."""
    percent = _context_percent(snapshot)
    bar_width = 20
    filled = int(bar_width * percent / 100) if percent <= 100 else bar_width
    empty = bar_width - filled
    bar = "▓" * filled + "░" * empty

    prepare_pct = _threshold_percent(snapshot, snapshot.context_prepare_threshold, 68)
    force_pct = _threshold_percent(snapshot, snapshot.context_force_threshold, 82)
    bar_style = _context_color(percent, prepare_percent=prepare_pct, force_percent=force_pct)

    def mouse_handler(mouse_event):
        event_type = str(getattr(mouse_event, "event_type", ""))
        if "MOUSE_UP" in event_type and on_context_click is not None:
            on_context_click()
        return None

    click = mouse_handler if on_context_click is not None else None
    return [
        ("bg:#2d2d3d #aaaaaa", " ctx ", click),
        (f"bg:#2d2d3d {bar_style}", bar, click),
        (f"bg:#2d2d3d {bar_style} bold", f" {percent}%", click),
        ("bg:#2d2d3d #888888", f"  {_context_token_label(snapshot)}", click),
        ("bg:#2d2d3d", " ", click),
    ]


def render_block_fragments(block: TranscriptBlock) -> StyleAndTextTuples:
    """Render a single transcript block as styled fragments with card/border."""
    kind = block.kind
    border_color = _KIND_BORDER_COLORS.get(kind, "#555555")
    card_bg = _KIND_CARD_BG.get(kind)

    # Tool and thought blocks get card treatment
    if card_bg is not None:
        return _render_card_block(block, border_color, card_bg)

    # User/assistant/other blocks get left bar treatment
    return _render_bar_block(block, border_color)


def _render_bar_block(block: TranscriptBlock, border_color: str) -> StyleAndTextTuples:
    """Render block with colored left bar."""
    result: StyleAndTextTuples = []
    status = f" [{block.status}]" if block.status else ""
    title_style = f"{border_color} bold"

    result.append((f"fg:{border_color}", "│ "))
    result.append((title_style, block.title))
    if status:
        status_color = _status_color(block.status)
        result.append((f"fg:{status_color}", status))
    result.append(("", "\n"))

    if block.body:
        for line in block.body.split("\n"):
            result.append((f"fg:{border_color}", "│ "))
            result.append(("", line))
            result.append(("", "\n"))

    return result


def _render_card_block(block: TranscriptBlock, border_color: str, card_bg: str) -> StyleAndTextTuples:
    """Render block as a card with border (for tool/thought)."""
    result: StyleAndTextTuples = []
    bg_style = f"bg:{card_bg}"
    border_style = f"fg:{border_color} {bg_style}"

    # Extract label from title (e.g., "tool result: run_bash" -> "tool")
    label = block.kind
    if ":" in block.title:
        label = block.title.split(":")[0].strip()

    # Top border
    result.append((border_style, "┌─"))
    result.append((f"{border_style} bold", f" {label} "))
    remaining = 50 - len(label) - 4
    result.append((border_style, "─" * max(remaining, 2)))
    result.append((border_style, "┐\n"))

    # Content lines
    status_icon = _status_icon(block.status)
    status_color = _status_color(block.status)

    # Title line (tool name, args summary)
    result.append((border_style, "│ "))
    result.append((f"fg:{status_color} {bg_style}", status_icon))
    result.append((f"{bg_style}", f" {block.title}"))
    result.append(("", "\n"))

    # Body lines
    if block.body:
        for line in block.body.split("\n"):
            result.append((border_style, "│ "))
            result.append((f"{bg_style} #cccccc", line))
            result.append(("", "\n"))

    # Bottom border
    result.append((border_style, "└"))
    result.append((border_style, "─" * 50))
    result.append((border_style, "┘"))
    result.append(("", "\n"))

    return result


def render_transcript_fragments(blocks: list[TranscriptBlock]) -> StyleAndTextTuples:
    """Render all transcript blocks for the main display area."""
    result: StyleAndTextTuples = []
    for i, block in enumerate(blocks):
        if i > 0:
            result.append(("", "\n"))
        result.extend(render_block_fragments(block))
    return result


def welcome_fragments(snapshot: SessionStatusSnapshot) -> StyleAndTextTuples:
    """Welcome message as styled fragments."""
    return [
        ("bold #3874cb", "Harness Code Agent"),
        (" #888888", " async TUI\n"),
        ("#888888", f"session {snapshot.session_id}  "),
        ("#888888", f"profile {snapshot.profile}  "),
        ("#888888", f"workspace {snapshot.cwd}\n"),
        ("#888888", "Use /help for commands. Ctrl-C cancels current turn.\n"),
        ("", "\n"),
    ]


def _status_color(status: str) -> str:
    if status in ("success", "approved"):
        return "#a3be8c"
    if status in ("failed", "denied"):
        return "#bf616a"
    if status in ("running", "pending"):
        return "#ebcb8b"
    return "#888888"


def _status_icon(status: str) -> str:
    if status in ("success", "approved"):
        return "✓"
    if status in ("failed", "denied"):
        return "✗"
    if status == "running":
        return "▶"
    if status == "pending":
        return "◆"
    if status == "thought":
        return "💭"
    return "·"
