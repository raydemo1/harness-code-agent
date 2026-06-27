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
            "name": "parallel",
            "description": (
                "Run up to 8 independent side-effect-safe tool calls concurrently. "
                "Use this to gather related repository, web, memory, or consultation context efficiently. "
                "Only read/network/subagent lanes are allowed; edits, shell commands, planning, user prompts, approvals, and long-running jobs are rejected."
            ),
            "parameters": {
                "type": "object",
                "required": ["tool_uses"],
                "properties": {
                    "tool_uses": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "description": "Independent tool uses to execute in parallel.",
                        "items": {
                            "type": "object",
                            "required": ["tool_name", "arguments"],
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Optional short label for this tool use.",
                                },
                                "tool_name": {
                                    "type": "string",
                                    "description": "Registered tool name, e.g. read_file, list_files, repo_search, web_search, consult_subagent.",
                                },
                                "arguments": {
                                    "type": "object",
                                    "description": "Arguments for the target tool.",
                                },
                            },
                        },
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
            "description": "Update tracked todo and acceptance state. skip mode does not call this tool. This tool does not create formal plan.md files or approval gates.",
            "parameters": {
                "type": "object",
                "required": ["mode", "update_kind", "goal", "steps", "current_step", "completed_steps", "blockers", "next_action", "requires_approval"],
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Use tracked for non-trivial work that needs todo/acceptance tracking. skip is the direct execution path and must not call this tool.",
                        "enum": ["tracked"],
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
                        "description": "Ignored by update_plan_state. Formal plan.md files belong to interactive planning flows, not tracked task execution.",
                    },
                    "replan_reason": {
                        "type": "string",
                        "description": "Required when update_kind is replan.",
                    },
                    "requires_approval": {
                        "type": "boolean",
                        "description": "Ignored by update_plan_state. Tracked execution never waits for approval through this tool.",
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
                    "acceptance_checks": {
                        "type": "array",
                        "maxItems": 10,
                        "description": "Optional on start. Concrete acceptance checks; IDs and origins are assigned by the framework.",
                        "items": {
                            "type": "object",
                            "required": ["text", "source", "verification_command"],
                            "properties": {
                                "text": {"type": "string"},
                                "source": {"type": "string", "maxLength": 300},
                                "verification_command": {"type": "string"},
                            },
                        },
                    },
                    "acceptance_revision": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Required when changing acceptance checks and on final updates.",
                    },
                    "acceptance_operations": {
                        "type": "array",
                        "description": "Atomic ordered add/update/remove operations. Every operation requires a reason.",
                        "items": {
                            "type": "object",
                            "required": ["operation", "reason"],
                            "properties": {
                                "operation": {"type": "string", "enum": ["add", "update", "remove"]},
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "source": {"type": "string", "maxLength": 300},
                                "verification_command": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                    "check_results": {
                        "type": "array",
                        "description": "Final per-check results. Success requires every current check exactly once with status passed.",
                        "items": {
                            "type": "object",
                            "required": ["id", "status", "summary"],
                            "properties": {
                                "id": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["passed", "failed", "not_run"],
                                },
                                "summary": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repo_search",
            "description": (
                "Search workspace code/text using bounded ripgrep. Use this for repository exploration instead of run_bash rg/grep/find commands. "
                "The harness always supplies an explicit path, short timeout, default generated-directory excludes, and max result limits."
            ),
            "parameters": {
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex pattern to search for."},
                    "path": {
                        "type": "string",
                        "description": "Relative file or directory path to search. Defaults to workspace root.",
                        "default": ".",
                    },
                    "glob": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional ripgrep glob filters, e.g. ['*.py'] or ['!*.lock'].",
                        "default": [],
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether search should be case-sensitive.",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Maximum result lines to return.",
                        "default": 100,
                    },
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "description": "Context lines around each match.",
                        "default": 0,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List workspace files and directories with bounded depth. Defaults to depth=2. "
                "Use higher depth only when you also provide bounded max_results and exclusions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default: root)",
                        "default": ".",
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum listing depth. depth=1 lists only direct children; default depth=2 includes one nested level.",
                        "default": 2,
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "description": "Maximum entries to return.",
                        "default": 200,
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "Include hidden dot paths except protected internal/generated defaults.",
                        "default": False,
                    },
                    "exclude": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional directory names to exclude.",
                        "default": [],
                    },
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
                "Do not use shell for repository search or file listing; use repo_search/list_files/read_file. "
                "Repository-browsing shell commands such as bare rg, recursive grep/findstr, Get-ChildItem -Recurse, or dir /s may be blocked or rewritten. "
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
