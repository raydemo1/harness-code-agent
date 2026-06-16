"""Pre-exit and static verification middleware."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .base import AgentMiddleware


log = logging.getLogger("harness")


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
        """Check if the agent has called any action tools."""
        action_tools = {"run_bash", "write_file", "consult_subagent"}
        start = getattr(runtime_state, "current_turn_start_index", 0) if runtime_state is not None else 0
        for msg in messages[start:]:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn_name = tc.get("function", {}).get("name", "")
                    if fn_name in action_tools:
                        return True
        return False

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

        # Gate 1: Agent hasn't done ANY work — force it to start
        if not has_worked:
            if _allows_text_only_exit(task_text):
                log.info("Pre-exit: allowing no-tool response for text-only task")
                return None
            log.warning(f"Pre-exit: agent wants to stop but has done NO work (attempt {self._exit_attempts})")
            if self._exit_attempts <= 3:  # give up to 3 chances to start working
                return (
                    "[SYSTEM] You have NOT completed the task. You have not executed any commands "
                    "or written any files yet.\n"
                    "You MUST use run_bash to execute commands and write_file to create output files.\n"
                    "Read the task requirements again and START WORKING. Do not just describe "
                    "what you would do — actually DO it using the available tools."
                )
            # After 3 attempts with no work, give up
            log.error("Pre-exit: agent refused to work after 3 attempts")
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


VERDICT_PASS = 0
VERDICT_WARN = 1
VERDICT_BLOCK = 2


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


def _allows_text_only_exit(task_text: str) -> bool:
    """Return True when the current turn is plainly conversational/read-only."""
    text = " ".join(str(task_text or "").strip().split())
    if not text:
        return False

    lowered = text.lower()
    if _is_identity_or_greeting(text, lowered):
        return True
    if _is_capability_question(text, lowered):
        return True
    if _has_actionable_workspace_intent(text, lowered):
        return False
    if _looks_like_question(text, lowered) and len(text) <= 160:
        return True
    if lowered.startswith(("explain ", "what is ", "what are ", "why ", "tell me ")):
        return True
    if text.startswith(("解释", "说明", "介绍", "什么是", "为什么")):
        return True
    return False


def _is_identity_or_greeting(text: str, lowered: str) -> bool:
    normalized = text.strip(" \t\r\n?？!！.。")
    if normalized in {
        "你是谁",
        "你是誰",
        "你是什么",
        "你是干什么的",
        "你能做什么",
        "你好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "hey",
        "who are you",
        "what are you",
        "what can you do",
    }:
        return True
    return bool(re.fullmatch(r"(hi|hello|hey)[!. ]*", lowered))


def _is_capability_question(text: str, lowered: str) -> bool:
    if "?" not in text and "？" not in text:
        return False
    if any(phrase in lowered for phrase in ("can you", "could you", "what can you do", "who are you")):
        return True
    return "你" in text and any(term in text for term in ("能", "可以", "会", "會")) and any(
        term in text for term in ("做什么", "干什么", "修改", "写代码", "运行", "测试", "帮我")
    )


def _has_actionable_workspace_intent(text: str, lowered: str) -> bool:
    chinese_action_terms = (
        "修复",
        "修改",
        "改动",
        "调整",
        "实现",
        "新增",
        "添加",
        "创建",
        "生成",
        "写",
        "运行",
        "执行",
        "测试",
        "检查",
        "查看",
        "审查",
        "评审",
        "调试",
        "诊断",
        "构建",
        "安装",
        "删除",
        "提交",
        "部署",
        "打开",
        "读取",
        "搜索",
        "列出",
    )
    english_action_terms = (
        "fix",
        "modify",
        "edit",
        "implement",
        "add",
        "create",
        "write",
        "run",
        "execute",
        "test",
        "check",
        "inspect",
        "review",
        "debug",
        "diagnose",
        "build",
        "install",
        "remove",
        "delete",
        "commit",
        "deploy",
        "open",
        "read",
        "search",
        "list",
        "refactor",
        "update",
    )
    if any(term in text for term in chinese_action_terms):
        return True
    if re.search(r"\b(" + "|".join(re.escape(term) for term in english_action_terms) + r")\b", lowered):
        return True

    local_terms = (
        "repo",
        "repository",
        "workspace",
        "project",
        "file",
        "path",
        "codebase",
        "仓库",
        "工作区",
        "项目",
        "文件",
        "目录",
        "代码库",
    )
    local_references = ("this", "current", "local", "here", "这个", "当前", "本地", "这里")
    if any(term in lowered or term in text for term in local_terms) and any(
        ref in lowered or ref in text for ref in local_references
    ):
        return True

    return bool(re.search(r"([A-Za-z]:\\|[/\\].+\.[A-Za-z0-9]{1,8}\b|\b\w+\.(py|js|ts|tsx|jsx|json|md|toml|yaml|yml|txt)\b)", text))


def _looks_like_question(text: str, lowered: str) -> bool:
    if text.endswith(("?", "？")):
        return True
    return lowered.startswith(("who ", "what ", "why ", "how ", "when ", "where ", "which "))
