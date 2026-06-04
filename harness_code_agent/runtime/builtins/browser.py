"""Headless browser testing tools."""
from __future__ import annotations

import subprocess
import time

from ... import config
from ..tool_result import ToolResult

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


_dev_server_proc: subprocess.Popen | None = None


def _ensure_dev_server(start_command: str, port: int, startup_wait: int = 8) -> str:
    """Start a dev server in the background if not already running."""
    global _dev_server_proc
    if _dev_server_proc is not None and _dev_server_proc.poll() is None:
        return f"Dev server already running (pid={_dev_server_proc.pid})"
    _dev_server_proc = subprocess.Popen(
        start_command,
        shell=True,
        cwd=config.WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(startup_wait)
    if _dev_server_proc.poll() is not None:
        stderr = _dev_server_proc.stderr.read().decode(errors="replace")[:2000]
        return f"[error] Dev server exited immediately: {stderr}"
    return f"Dev server started (pid={_dev_server_proc.pid}, port={port})"


def stop_dev_server() -> ToolResult:
    """Stop the background dev server."""
    global _dev_server_proc
    if _dev_server_proc is None:
        return ToolResult(
            tool="stop_dev_server",
            status="success",
            output="No dev server running",
            metadata={"status_source": "native"},
        )
    _dev_server_proc.terminate()
    try:
        _dev_server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _dev_server_proc.kill()
    _dev_server_proc = None
    return ToolResult(
        tool="stop_dev_server",
        status="success",
        output="Dev server stopped",
        metadata={"status_source": "native"},
    )


def browser_test(
    url: str,
    actions: list[dict] | None = None,
    screenshot: bool = True,
    start_command: str | None = None,
    port: int = 5173,
    startup_wait: int = 8,
) -> ToolResult:
    """
    Launch a headless browser, navigate to a URL, perform actions, and
    optionally take a screenshot. Returns a text report of what happened.

    actions is a list of dicts, each with:
      - type: "click" | "fill" | "wait" | "evaluate" | "scroll"
      - selector: CSS selector (for click/fill)
      - value: text to type (for fill), JS code (for evaluate)
      - delay: ms to wait (for wait)

    If start_command is provided, starts a dev server first.
    """
    if not HAS_PLAYWRIGHT:
        output = (
            "[error] Playwright not installed. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )
        return ToolResult(
            tool="browser_test",
            status="failed",
            output=output,
            error="Playwright not installed",
            metadata={"status_source": "runtime"},
        )

    report_lines = []
    failed = False
    error_message = None

    # Optionally start dev server
    if start_command:
        srv_result = _ensure_dev_server(start_command, port, startup_wait)
        report_lines.append(f"Server: {srv_result}")
        if srv_result.startswith("[error]"):
            failed = True
            error_message = srv_result.removeprefix("[error] ")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})

            # Navigate
            try:
                page.goto(url, timeout=15000)
                report_lines.append(f"Navigated to {url} — title: {page.title()}")
            except Exception as e:
                report_lines.append(f"[error] Navigation failed: {e}")
                browser.close()
                return ToolResult(
                    tool="browser_test",
                    status="failed",
                    output="\n".join(report_lines),
                    error=f"Navigation failed: {e}",
                    metadata={"url": url, "status_source": "browser"},
                )

            # Check for console errors
            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            # Execute actions
            for action in (actions or []):
                action_type = action.get("type", "")
                selector = action.get("selector", "")
                value = action.get("value", "")
                delay = action.get("delay", 1000)

                try:
                    if action_type == "click":
                        page.click(selector, timeout=5000)
                        report_lines.append(f"Clicked: {selector}")
                    elif action_type == "fill":
                        page.fill(selector, value, timeout=5000)
                        report_lines.append(f"Filled '{selector}' with '{value[:50]}'")
                    elif action_type == "wait":
                        page.wait_for_timeout(delay)
                        report_lines.append(f"Waited {delay}ms")
                    elif action_type == "evaluate":
                        result = page.evaluate(value)
                        report_lines.append(f"JS eval result: {str(result)[:500]}")
                    elif action_type == "scroll":
                        page.evaluate(f"window.scrollBy(0, {value or 500})")
                        report_lines.append(f"Scrolled by {value or 500}px")
                    else:
                        report_lines.append(f"[warn] Unknown action type: {action_type}")
                except Exception as e:
                    report_lines.append(f"[error] Action {action_type}('{selector}'): {e}")
                    failed = True
                    error_message = f"Action {action_type}('{selector}'): {e}"

                page.wait_for_timeout(300)  # brief pause between actions

            # Gather page info
            report_lines.append(f"Final URL: {page.url}")
            report_lines.append(f"Visible text (first 2000 chars): {page.inner_text('body')[:2000]}")

            if console_errors:
                report_lines.append(f"Console errors ({len(console_errors)}):")
                for err in console_errors[:10]:
                    report_lines.append(f"  - {err[:200]}")

            # Screenshot
            if screenshot:
                ss_path = Path(config.WORKSPACE) / "_screenshot.png"
                page.screenshot(path=str(ss_path), full_page=False)
                report_lines.append(f"Screenshot saved to _screenshot.png")

            browser.close()

    except Exception as e:
        report_lines.append(f"[error] Browser test failed: {e}")
        failed = True
        error_message = f"Browser test failed: {e}"

    return ToolResult(
        tool="browser_test",
        status="failed" if failed else "success",
        output="\n".join(report_lines),
        error=error_message,
        metadata={"url": url, "status_source": "browser"},
    )
