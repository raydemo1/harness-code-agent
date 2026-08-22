"""Error guidance middleware."""
from __future__ import annotations

import logging
import os

from ... import config
from ..tool_result import ToolResult
from .base import AgentMiddleware


log = logging.getLogger("harness")


class ErrorGuidanceMiddleware(AgentMiddleware):
    """
    Detects common error patterns in tool output and injects specific,
    actionable recovery suggestions.

    Weak models struggle to recover from errors on their own — they often
    retry the same failing command or give up. This middleware matches
    error patterns and provides concrete next steps.

    Based on TB2 command-level error analysis:
      - 24.1% of failures: command not found / not on PATH
      -  9.6% of failures: runtime errors in executables
      -  High rate: permission denied, missing dependencies
    """

    # Pattern → (description, recovery suggestion)
    # Patterns are checked in order; first match wins.
    ERROR_PATTERNS: list[tuple[str, str, str]] = [
        # --- Command not found ---
        (
            "command not found",
            "command_not_found",
            "The command is not installed. Try:\n"
            "  apt-get update && apt-get install -y <package>  (for system tools)\n"
            "  pip install <package>  (for Python tools)\n"
            "  which <command> || apt-cache search <keyword>  (to find the right package)\n"
            "If apt-get fails with permission denied, prefix with sudo.",
        ),
        (
            "no such file or directory",
            "file_not_found",
            "A file or directory doesn't exist. Check:\n"
            "  ls -la <parent_directory>  (does the path exist?)\n"
            "  pwd  (are you in the right directory?)\n"
            "  find . -name '<filename>'  (search for the file)",
        ),
        # --- Permission errors ---
        (
            "permission denied",
            "permission_denied",
            "Permission denied. Try:\n"
            "  chmod +x <file>  (if it needs to be executable)\n"
            "  sudo <command>  (if it needs root)\n"
            "  ls -la <file>  (check current permissions)",
        ),
        # --- Python/pip errors ---
        (
            "externally-managed-environment",
            "pip_managed_env",
            "This Python environment is externally managed (PEP 668). Use:\n"
            "  pip install --break-system-packages <package>\n"
            "  or: pip install --user <package>\n"
            "  or: python3 -m venv /tmp/venv && source /tmp/venv/bin/activate",
        ),
        (
            "modulenotfounderror",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "  pip install --break-system-packages <module_name>  (if managed env)\n"
            "Check the exact package name — it may differ from the import name.",
        ),
        (
            "no module named",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "Check: the pip package name may differ from the import name "
            "(e.g. 'import cv2' → 'pip install opencv-python').",
        ),
        # --- Compilation errors ---
        (
            "fatal error:",
            "compilation",
            "Compilation failed. Check:\n"
            "  1. Read the error — it shows the file and line number.\n"
            "  2. Missing header? Install dev packages: apt-get install -y lib<name>-dev\n"
            "  3. Use: apt-cache search <header_name> to find the right package.",
        ),
        (
            "undefined reference to",
            "linker",
            "Linker error — a symbol is missing. Check:\n"
            "  1. Are you linking all required libraries? (-l<lib> flag)\n"
            "  2. Is the library installed? apt-get install -y lib<name>-dev\n"
            "  3. Check library search path: ldconfig -p | grep <lib>",
        ),
        # --- Git errors ---
        (
            "not a git repository",
            "git",
            "Not in a git repository. Try:\n"
            "  git init  (to create one)\n"
            "  cd <correct_directory>  (you may be in the wrong dir)\n"
            "  find / -name '.git' -type d 2>/dev/null  (find existing repos)",
        ),
        # --- Disk/resource errors ---
        (
            "no space left on device",
            "disk_full",
            "Disk is full. Free space:\n"
            "  df -h  (check disk usage)\n"
            "  du -sh /* 2>/dev/null | sort -rh | head  (find large dirs)\n"
            "  apt-get clean  (clear package cache)\n"
            "  rm -rf /tmp/*  (clear temp files)",
        ),
        (
            "killed",
            "oom",
            "Process was killed (likely out of memory). Try:\n"
            "  free -h  (check available memory)\n"
            "  Reduce memory usage: smaller batch size, fewer workers, etc.\n"
            "  Use swap: fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile",
        ),
    ]

    def __init__(self):
        self._last_guidance_type: str | None = None

    @staticmethod
    def _for_active_shell(guidance_type: str, default: str) -> str:
        """Return recovery commands that match the explicitly selected shell."""
        if os.name != "nt" or (config.WINDOWS_SHELL or "pwsh").strip().lower() != "pwsh":
            return default

        powershell_guidance = {
            "command_not_found": (
                "The command is not installed or is not on PATH. Try:\n"
                "  Get-Command <command> -ErrorAction SilentlyContinue\n"
                "  winget search <package>\n"
                "  winget install <package>  (after confirming the package id)\n"
                "For Python tools, use: python -m pip install <package>."
            ),
            "file_not_found": (
                "A file or directory doesn't exist. Check:\n"
                "  Get-Location\n"
                "  Test-Path -LiteralPath '<path>'\n"
                "  Get-ChildItem -LiteralPath '<parent>' -Force\n"
                "  Get-ChildItem -Recurse -Filter '<filename>'"
            ),
            "permission_denied": (
                "Access was denied. Check the resolved path and current ACL with:\n"
                "  Resolve-Path -LiteralPath '<path>'\n"
                "  Get-Acl -LiteralPath '<path>' | Format-List\n"
                "Only relaunch pwsh as Administrator when the operation truly requires elevation."
            ),
            "git": (
                "Not in a git repository. Check:\n"
                "  Get-Location\n"
                "  git rev-parse --show-toplevel\n"
                "  Get-ChildItem -Path .. -Directory -Filter .git -Recurse -ErrorAction SilentlyContinue"
            ),
            "disk_full": (
                "Disk space is exhausted. Inspect it with:\n"
                "  Get-Volume\n"
                "  Get-PSDrive -PSProvider FileSystem\n"
                "Review large files before removing anything."
            ),
            "oom": (
                "The process may have exceeded available memory. Check Task Manager or:\n"
                "  Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory,TotalVisibleMemorySize\n"
                "Then reduce batch size, workers, or process concurrency."
            ),
        }
        return powershell_guidance.get(guidance_type, default)

    def post_tool(self, tool_name: str, tool_args: dict, result: ToolResult,
                  messages: list[dict], runtime_state=None,
                  agent_name: str | None = None) -> str | None:
        if tool_name != "run_bash" or result.status != "failed":
            return None

        result_lower = (result.error or result.output or "").lower()

        for pattern, guidance_type, suggestion in self.ERROR_PATTERNS:
            if pattern in result_lower:
                # Don't repeat the same guidance type consecutively
                if guidance_type == self._last_guidance_type:
                    return None
                self._last_guidance_type = guidance_type
                log.info(f"Error guidance: matched '{guidance_type}'")
                suggestion = self._for_active_shell(guidance_type, suggestion)
                return f"[SYSTEM] Error detected — here's how to fix it:\n{suggestion}"

        self._last_guidance_type = None
        return None
