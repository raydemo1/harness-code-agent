"""OpenAI function-calling schemas for built-in tools."""
from __future__ import annotations

import os

from ... import config
from .filesystem import READ_FILE_MAX_LINES


CORE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file. Prefer bounded reads with start_line and max_lines unless the file is known small.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based starting line for a bounded read.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": READ_FILE_MAX_LINES,
                        "description": "Maximum lines to return for a bounded read. Must be <= 500.",
                    },
                    "include_line_numbers": {
                        "type": "boolean",
                        "description": "Prefix returned lines with 1-based line numbers.",
                        "default": False,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "Read a skill file from the skills/ directory. Use this to load a skill's SKILL.md or any sub-files referenced within it. Path should be relative to project root (e.g. 'skills/frontend-design/SKILL.md').",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path to skill file (e.g. 'skills/frontend-design/SKILL.md')"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": "Search hidden deferred tools available to the current profile. Matching tools are revealed for future tool calls in this conversation.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of the tool capability needed.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                        "description": "Maximum number of matching hidden tools to reveal.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "File content to write"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a safe text patch to one file. The search text must match exactly once, or the patch fails without modifying the file.",
            "parameters": {
                "type": "object",
                "required": ["path", "search", "replace"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "search": {"type": "string", "description": "Existing text to replace. Must match exactly once."},
                    "replace": {"type": "string", "description": "Replacement text"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_state",
            "description": "Update light/full planning state. skip mode does not call this tool. light writes only session state.json; full also writes global_plan/current/plan.md when approval is required.",
            "parameters": {
                "type": "object",
                "required": ["mode", "update_kind", "goal", "steps", "current_step", "completed_steps", "blockers", "next_action", "requires_approval"],
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Planning mode selected by the agent self-check. skip is the direct execution path and must not call this tool.",
                        "enum": ["light", "full"],
                    },
                    "update_kind": {
                        "type": "string",
                        "description": "Planning update kind.",
                        "enum": ["start", "progress", "replan", "final"],
                    },
                    "goal": {"type": "string", "description": "Overall task goal"},
                    "steps": {
                        "type": "array",
                        "description": "Ordered list of planned steps",
                        "items": {"type": "string"},
                    },
                    "current_step": {"type": "string", "description": "The step being worked on now"},
                    "completed_steps": {
                        "type": "array",
                        "description": "Steps already completed",
                        "items": {"type": "string"},
                    },
                    "blockers": {
                        "type": "array",
                        "description": "Current blockers, if any",
                        "items": {"type": "string"},
                    },
                    "next_action": {"type": "string", "description": "The exact next action to take. May be empty or 'none' for final updates."},
                    "plan_markdown": {
                        "type": "string",
                        "description": "Full plan.md content. Required for full start and for requires_approval=true replan.",
                    },
                    "replan_reason": {
                        "type": "string",
                        "description": "Required when update_kind is replan.",
                    },
                    "requires_approval": {
                        "type": "boolean",
                        "description": "true writes plan.md and waits for user confirmation; false only updates state.json and continues.",
                    },
                    "result_status": {
                        "type": "string",
                        "description": "Required for final updates: success, partial, blocked, failed, or another concise status.",
                    },
                    "validation": {
                        "type": "string",
                        "description": "Required for final updates. Summarize validation commands/results or why validation could not run.",
                    },
                    "remaining_issues": {
                        "type": "array",
                        "description": "Required for final updates. Empty list means no known remaining issues.",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in a directory recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default: root)",
                        "default": ".",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user one focused multiple-choice question. "
                "Use when the model needs a user decision before continuing. "
                "The UI always includes an Other/其他 option with free-text input, "
                "and returns the selected option as structured JSON."
            ),
            "parameters": {
                "type": "object",
                "required": ["question", "options"],
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to show the user.",
                    },
                    "options": {
                        "type": "array",
                        "description": (
                            "Choices to show before the automatic Other choice. "
                            "Pass concise labels with optional values/descriptions. "
                            "3-4 options is ideal; maximum 9 (only the first 9 get number-key shortcuts)."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["label"],
                            "properties": {
                                "label": {"type": "string", "description": "Short visible option label."},
                                "value": {"type": "string", "description": "Value returned to the model if selected."},
                                "description": {"type": "string", "description": "Optional one-line explanation."},
                            },
                        },
                    },
                    "other_label": {
                        "type": "string",
                        "description": "Label for the automatic free-text option. Defaults to 其他.",
                        "default": "其他",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "Search long-term project memory for relevant past decisions, "
                "debugging notes, commands, preferences, and learnings. Returns summaries; "
                "use read_memory_file for full details."
            ),
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_memory",
            "description": (
                "Queue a long-term memory candidate. This tool never writes MEMORY.md, "
                "manifest.json, dream-log.md, records.jsonl, or Markdown memory files directly; "
                "Dream merges candidates later."
            ),
            "parameters": {
                "type": "object",
                "required": ["summary"],
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Concise durable memory to preserve.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title for the memory candidate.",
                        "default": "",
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional Dream routing hint: project.md, decisions.md, commands.md, "
                            "debugging.md, preferences.md, or learnings.md."
                        ),
                        "default": "",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for later recall.",
                        "default": [],
                    },
                    "source_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Workspace paths related to this memory.",
                        "default": [],
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "Confidence that the memory is durable and useful.",
                        "default": 0.7,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory_file",
            "description": "Read a generated long-term memory file for details after MEMORY.md navigation or recall points to it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Readable memory file.",
                        "enum": [
                            "MEMORY.md",
                            "project.md",
                            "decisions.md",
                            "commands.md",
                            "debugging.md",
                            "preferences.md",
                            "learnings.md",
                            "dream-log.md",
                        ],
                        "default": "MEMORY.md",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Execute a shell command in the workspace directory. "
                + (
                    "When HARNESS_SANDBOX_MODE=docker this runs inside a Linux Bash Docker sandbox with the workspace mounted at /workspace. "
                    if (config.SANDBOX_MODE or "host").strip().lower() == "docker"
                    else ""
                )
                + (
                    "On Windows this runs PowerShell by default; prefer PowerShell syntax, or cmd.exe syntax only when HARNESS_WINDOWS_SHELL=cmd. "
                    if os.name == "nt"
                    else "On POSIX this runs a shell suitable for standard Bash-style commands. "
                )
                + "Use for installing deps, running builds, starting servers, running tests, etc. "
                "For long-running verification commands (compilation, training), increase the timeout parameter. "
                "For dev servers, watch mode, and runserver commands, this returns a background shell job id; use read_shell_output, list_shell_jobs, and stop_shell_job to manage it. "
                "Prefer bounded inspection commands such as rg, head/tail, sed -n, Select-Object -First/-Last, or line counts instead of dumping whole files or logs. "
                "Stderr is preserved separately in output for easier debugging."
            ),
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run; keep inspection commands bounded."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 300). Increase for long builds/training.",
                        "default": 300,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_shell_jobs",
            "description": "List background shell jobs started by long-running run_bash commands in the current session.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_shell_output",
            "description": "Read recent stdout/stderr output from a background shell job.",
            "parameters": {
                "type": "object",
                "required": ["job_id"],
                "properties": {
                    "job_id": {"type": "string", "description": "Background shell job id returned by run_bash."},
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum recent output characters to return (default 12000, capped at 100000).",
                        "default": 12000,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_shell_job",
            "description": "Stop a background shell job and its child process tree.",
            "parameters": {
                "type": "object",
                "required": ["job_id"],
                "properties": {
                    "job_id": {"type": "string", "description": "Background shell job id returned by run_bash."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_subagent",
            "description": (
                "Ask a read-only consultation sub-agent for findings, evidence, recommendations, and risks. "
                "Use only for local codebase investigation, parallel search, test design, or review. "
                "The main agent owns all code changes, final integration, verification, and stop decisions."
            ),
            "parameters": {
                "type": "object",
                "required": ["task"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Detailed read-only consultation request",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["codebase_investigation", "parallel_search", "test_design", "review"],
                        "description": "Consultation mode",
                        "default": "codebase_investigation",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information. Use when you need documentation, examples, or domain knowledge not available locally. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 5)",
                        "default": 5,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a web page. Use after web_search to read a specific page in detail.",
            "parameters": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
            },
        },
    },
]


# --- Evaluator-only tools (browser testing) ---

BROWSER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_test",
            "description": (
                "Launch a headless Chromium browser to test the running application. "
                "Navigates to a URL, performs UI actions (click, fill, scroll, evaluate JS), "
                "captures console errors, and takes a screenshot. "
                "Optionally starts a dev server first via start_command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to (e.g. http://localhost:5173)",
                    },
                    "actions": {
                        "type": "array",
                        "description": "List of browser actions to perform sequentially",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "fill", "wait", "evaluate", "scroll"],
                                    "description": "Action type",
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector (for click/fill)",
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Text for fill, JS code for evaluate, pixels for scroll",
                                },
                                "delay": {
                                    "type": "integer",
                                    "description": "Milliseconds to wait (for wait action)",
                                },
                            },
                        },
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a screenshot after actions (default: true)",
                        "default": True,
                    },
                    "start_command": {
                        "type": "string",
                        "description": "Shell command to start the dev server (e.g. 'npm run dev'). Only needed on first call.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Port the dev server runs on (default: 5173)",
                        "default": 5173,
                    },
                    "startup_wait": {
                        "type": "integer",
                        "description": "Seconds to wait for dev server to start (default: 8)",
                        "default": 8,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_dev_server",
            "description": "Stop the background dev server started by browser_test.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
