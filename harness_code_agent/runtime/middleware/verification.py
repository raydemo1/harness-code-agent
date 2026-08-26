"""Pre-exit and static verification middleware."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .base import AgentMiddleware

log = logging.getLogger("harness")


@dataclass(frozen=True)
class ExitIntentDecision:
    mode: str
    confidence: float = 0.0
    reason: str = ""

    @property
    def should_continue(self) -> bool:
        return self.mode == "continue" and self.confidence >= 0.75


class PreExitVerificationMiddleware(AgentMiddleware):
    """
    Forces the agent to run a verification pass before it's allowed to stop.

    Three-level exit gate:
    1. First exit attempt with NO tool calls ever made → force agent to start working
    2. First exit attempt after some work → force verification pass
    3. Second exit attempt after verification → allow exit

    This prevents the "3-second exit" problem where weak models return text
    without calling any tools, and PreExitVerification lets them go after
    just one retry.
    """

    def __init__(self, verification_prompt: str | None = None,
                 include_task_requirements: bool = True):
        self._exit_attempts = 0
        self._verification_prompt = verification_prompt
        self._include_task_requirements = include_task_requirements

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        self._exit_attempts = 0

    @staticmethod
    def _current_user_task(messages: list[dict], runtime_state=None) -> str | None:
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        user_messages: list[str] = []
        for msg in messages[start:]:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            content = content.strip()
            if not content or _is_middleware_user_message(content):
                continue
            user_messages.append(content)
        return user_messages[-1] if user_messages else None

    @staticmethod
    def _has_done_work(messages: list[dict], runtime_state=None) -> bool:
        """Check if the agent has used any non-planning tool this turn."""
        ignored_tools = {"update_plan_state"}
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        for msg in messages[start:]:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn_name = tc.get("function", {}).get("name", "")
                    if fn_name and fn_name not in ignored_tools:
                        return True
        return False

    @staticmethod
    def _tool_names_used(messages: list[dict], runtime_state=None) -> list[str]:
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        names: list[str] = []
        for msg in messages[start:]:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []):
                fn_name = tc.get("function", {}).get("name", "")
                if fn_name:
                    names.append(fn_name)
        return names

    @staticmethod
    def _last_assistant_text(messages: list[dict], runtime_state=None) -> str:
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        for msg in reversed(messages[start:]):
            if msg.get("role") == "assistant":
                content = msg.get("content") or ""
                return content if isinstance(content, str) else ""
        return ""

    @staticmethod
    def _extract_task_requirements(messages: list[dict], runtime_state=None) -> str | None:
        """Extract the current turn's task requirements from the conversation."""
        content = PreExitVerificationMiddleware._current_user_task(messages, runtime_state)
        if content:
            if len(content) > 3000:
                content = content[:3000] + "\n... (truncated)"
            return content
        return None

    def pre_exit(self, messages: list[dict], runtime_state=None,
                 agent_name: str | None = None) -> str | None:
        self._exit_attempts += 1
        has_worked = self._has_done_work(messages, runtime_state)
        task_text = self._current_user_task(messages, runtime_state) or ""
        assistant_text = self._last_assistant_text(messages, runtime_state)
        tool_names = self._tool_names_used(messages, runtime_state)
        decision = classify_exit_intent(
            user_task=task_text,
            assistant_text=assistant_text,
            tool_names=tool_names,
        )
        if not decision.should_continue:
            log.info(
                "Pre-exit: allowing exit after intent gate mode=%s confidence=%.2f reason=%s",
                decision.mode,
                decision.confidence,
                decision.reason,
            )
            return None

        # Gate 1: Agent hasn't done ANY work — force it to start
        if not has_worked:
            log.warning(f"Pre-exit: agent wants to stop but has done NO work (attempt {self._exit_attempts})")
            if self._exit_attempts == 1:
                return (
                    "[SYSTEM] The user request appears to require workspace action before answering.\n"
                    "Continue with the smallest relevant tool action, such as inspecting files or "
                    "running a check. Edit or create files only when the user explicitly requested "
                    "a change or a file edit is necessary to satisfy the task."
                )
            return None

        # Gate 2: Agent has done work, first exit → force verification
        if self._exit_attempts == 1:
            log.info("Pre-exit verification: forcing verification pass")

            parts = []
            parts.append(
                "[SYSTEM] MANDATORY VERIFICATION — You are about to finish, "
                "but you MUST verify your work first."
            )

            if self._include_task_requirements:
                task_text = self._extract_task_requirements(messages, runtime_state)
                if task_text:
                    parts.append(
                        "\n--- ORIGINAL TASK REQUIREMENTS (verify against these, not your memory) ---\n"
                        f"{task_text}\n"
                        "--- END ORIGINAL TASK REQUIREMENTS ---"
                    )

            if self._verification_prompt:
                parts.append(f"\n{self._verification_prompt}")
            else:
                parts.append(
                    "\nDo NOT just re-read your code. Run actual test/check commands:\n"
                    "1. Go through EACH requirement above one by one.\n"
                    "2. For each, run a concrete verification command "
                    "(cat, ls -la, test -f, diff, grep, python3 -c, etc.)\n"
                    "3. Compare ACTUAL output against what the task asked for.\n"
                    "4. Pay special attention to exact formats, column orders, "
                    "file paths, and edge-case rules mentioned in the task.\n"
                    "5. If ANY check fails, fix it before stopping.\n"
                    "Think like an automated test script — would your solution pass?"
                )

            return "\n".join(parts)

        # Gate 3: Agent has done work and verified → allow exit
        log.info("Pre-exit verification: agent verified, allowing exit")
        return None



class StaticVerifierMiddleware(AgentMiddleware):
    """Pre-exit lint gate for Python files changed in the current turn.

    - ``py_compile`` (stdlib): syntax errors on any changed .py → block
    - ``ruff --diff`` (optional): only reports errors on *new/changed lines*,
      E/F → block, W/C/N → warn.  Gracefully skipped if ruff is not installed.
    """

    def __init__(self, workspace_root: str | None = None, workspace=None):
        self._workspace_root = workspace_root
        self._workspace = workspace
        self._turn_changed_start = len(getattr(workspace, "changed_files", [])) if workspace is not None else 0
        self._reported_warning_signatures: set[tuple[str, ...]] = set()

    def begin_turn(self, task: str, messages: list[dict], runtime_state=None,
                   agent_name: str | None = None) -> None:
        if self._workspace is not None:
            self._turn_changed_start = len(getattr(self._workspace, "changed_files", []))
        self._reported_warning_signatures.clear()

    def pre_exit(self, messages: list[dict], runtime_state=None,
                 agent_name: str | None = None) -> str | None:
        py_files = _turn_changed_py_files(
            self._workspace_root,
            self._workspace,
            self._turn_changed_start,
        )
        if not py_files:
            return None

        blocks: list[str] = []
        warns: list[str] = []

        # --- py_compile: syntax errors on changed files ---
        for path, msg in _check_py_compile(self._workspace_root, py_files):
            blocks.append(f"  [syntax] {path}: {msg}")

        # --- ruff --diff: only errors on changed lines, E/F → block ---
        for path, code, msg in _check_ruff_diff(self._workspace_root):
            line = f"  [{code}] {path}: {msg}" if path else f"  [{code}] {msg}"
            if code and code[0] in {"E", "F"}:
                blocks.append(line)
            else:
                warns.append(line)

        if blocks:
            details = "\n".join(blocks[:20])
            return (
                "[SYSTEM] LINT CHECK FAILED -- fix these errors before stopping:\n"
                f"{details}"
            )
        if warns:
            details = "\n".join(warns[:20])
            signature = tuple(warns[:20])
            if signature in self._reported_warning_signatures:
                return None
            self._reported_warning_signatures.add(signature)
            return (
                f"[SYSTEM] Lint warnings (non-blocking):\n{details}\n"
                "Consider fixing before stopping."
            )
        return None


def _turn_changed_py_files(workspace_root: str | None, workspace, start_index: int) -> list[str]:
    if workspace is None:
        return _git_diff_changed_py_files(workspace_root)
    root = Path(workspace_root or getattr(workspace, "root", ".")).resolve()
    changed = getattr(workspace, "changed_files", [])[start_index:]
    files: set[str] = set()
    for path in changed:
        rel = Path(path)
        rel_text = rel.as_posix()
        if rel_text.endswith(".py") and (root / rel).exists():
            files.add(rel_text)
    return sorted(files)


def _git_diff_changed_py_files(workspace_root: str | None) -> list[str]:
    """Return .py files with uncommitted changes (tracked + untracked)."""
    import subprocess
    if not workspace_root:
        return []
    files: list[str] = []
    # Tracked changes (modified, added, renamed)
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=workspace_root,
            check=False,
        )
        if result.returncode == 0:
            files.extend(result.stdout.splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Untracked files
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
            check=False,
            cwd=workspace_root,
        )
        if result.returncode == 0:
            files.extend(result.stdout.splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return list({f.strip().replace("\\", "/") for f in files if f.strip().endswith(".py")})


def _check_py_compile(
    workspace_root: str | None, py_files: list[str],
) -> list[tuple[str, str]]:
    """Parse each file for syntax errors without writing bytecode."""
    import ast
    errors: list[tuple[str, str]] = []
    for rel_path in py_files:
        full_path = Path(workspace_root) / rel_path if workspace_root else Path(rel_path)
        try:
            source = full_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=str(full_path))
        except (OSError, SyntaxError) as exc:
            errors.append((rel_path, str(exc)))
    return errors


def _check_ruff_diff(workspace_root: str | None) -> list[tuple[str, str, str]]:
    """Run ``ruff check --diff`` — only reports findings on changed lines."""
    import subprocess
    if not workspace_root:
        return []
    try:
        result = subprocess.run(
            ["ruff", "check", "--diff", "--no-fix", "--output-format=text"],
            check=False,
            capture_output=True, text=True, timeout=30,
            cwd=workspace_root,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return [("", "TIMEOUT", "ruff check timed out after 30s")]
    if result.returncode == 0:
        return []
    findings: list[tuple[str, str, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(":", 3)
        if len(parts) >= 4:
            path = parts[0].strip()
            rest = parts[3].strip()
            code_end = rest.find(" ")
            if code_end > 0:
                code, msg = rest[:code_end], rest[code_end + 1:]
            else:
                code, msg = rest, ""
            findings.append((path, code, msg))
        else:
            findings.append(("", "", line))
    return findings


def _is_middleware_user_message(content: str) -> bool:
    stripped = content.lstrip()
    return stripped.startswith(("[SYSTEM]", "[blocked]"))


def classify_exit_intent(
    *,
    user_task: str,
    assistant_text: str = "",
    tool_names: list[str] | None = None,
) -> ExitIntentDecision:
    """Use a small LLM gate to decide whether the agent should continue.

    The safe fallback is intentionally permissive: if the gate is unavailable,
    ambiguous, or malformed, allow the assistant to exit instead of pushing it
    into unnecessary tool calls.
    """
    user_task = str(user_task or "").strip()
    if not user_task:
        return ExitIntentDecision(mode="exit", reason="empty user task")

    try:
        from ... import config
        from ...agent.providers import ProviderAdapter, client_scope

        profile = config.resolve_model_profile("fast")
        adapter = ProviderAdapter(profile.provider)
        with client_scope() as client:
            response = client.chat.completions.create(**adapter.chat_kwargs(
                profile=profile,
                messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an exit gate for a local coding agent. Decide whether "
                        "the agent may stop now or should continue with tools.\n"
                        "Return JSON only with keys: mode, confidence, reason.\n"
                        "mode must be exactly \"exit\" or \"continue\".\n"
                        "Be lenient: choose exit for greetings, identity/capability "
                        "questions, conceptual explanations, general advice, and anything "
                        "that can be honestly answered from the conversation.\n"
                        "Choose continue only when the user's request cannot be satisfied "
                        "without actual local workspace action: inspecting repository state, "
                        "running commands/tests/builds, editing code, producing an on-disk "
                        "artifact, or verifying a concrete local result.\n"
                        "Do not require file edits merely because tools may be useful. "
                        "If ambiguous, choose exit."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_task": user_task,
                            "assistant_about_to_send": str(assistant_text or "")[:2000],
                            "tools_used_this_turn": list(tool_names or []),
                        },
                        ensure_ascii=False,
                    ),
                },
                ],
                max_tokens=160,
            ))
        raw = response.choices[0].message.content or ""
        return _parse_exit_intent_decision(raw)
    except Exception as exc:
        log.info("Pre-exit intent gate failed open: %s", exc)
        return ExitIntentDecision(mode="exit", reason="intent gate unavailable")


def _parse_exit_intent_decision(raw: str) -> ExitIntentDecision:
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ExitIntentDecision(mode="exit", reason="invalid gate JSON")

    mode = str(data.get("mode") or "exit").strip().lower()
    if mode not in {"exit", "continue"}:
        mode = "exit"
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or "").strip()[:300]
    return ExitIntentDecision(mode=mode, confidence=confidence, reason=reason)
