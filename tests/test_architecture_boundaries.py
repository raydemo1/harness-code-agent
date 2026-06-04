import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "harness_code_agent"


LEGACY_FACADES = {
    PACKAGE_ROOT / "agent" / "loop.py",
    PACKAGE_ROOT / "runtime" / "middlewares.py",
    PACKAGE_ROOT / "runtime" / "tools.py",
}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)


def _resolve_import(current_module: str, level: int, module: str | None) -> str:
    if level == 0:
        return module or ""
    parts = current_module.split(".")[:-1]
    if level > 1:
        parts = parts[: -(level - 1)] if level - 1 <= len(parts) else []
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _imports(path: Path) -> list[str]:
    current_module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
            continue
        if isinstance(node, ast.ImportFrom):
            module = _resolve_import(current_module, node.level, node.module)
            if module:
                imports.append(module)
                imports.extend(f"{module}.{alias.name}" for alias in node.names)
    return imports


def test_internal_production_code_uses_narrow_modules_not_legacy_facades():
    legacy_modules = {
        "harness_code_agent.agent.loop",
        "harness_code_agent.runtime.middlewares",
        "harness_code_agent.runtime.tools",
    }
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path in LEGACY_FACADES:
            continue
        imports = set(_imports(path))
        if imports & legacy_modules:
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))

    assert offenders == []


def test_runtime_modules_do_not_depend_on_agent_loop_facade():
    offenders: list[str] = []
    for path in (PACKAGE_ROOT / "runtime").rglob("*.py"):
        if path in LEGACY_FACADES:
            continue
        if "harness_code_agent.agent.loop" in set(_imports(path)):
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))

    assert offenders == []


def test_legacy_facades_keep_public_api():
    from harness_code_agent.agent import loop
    from harness_code_agent.runtime import middlewares, tools

    tool_names = [
        "BUILTIN_TOOL_REGISTRY",
        "TOOL_SCHEMAS",
        "BROWSER_TOOL_SCHEMAS",
        "TOOL_DISPATCH",
        "ToolRegistry",
        "ToolExecutionLane",
        "execute_tool",
        "execute_tool_result",
        "tool_schemas_for_profile",
        "consult_subagent",
        "ConsultationReadOnlyMiddleware",
    ]
    middleware_names = [
        "AgentMiddleware",
        "LoopDetectionMiddleware",
        "RecoveryStrategyMiddleware",
        "StaticVerifierMiddleware",
        "TaskTrackingEnforcementMiddleware",
        "TimeBudgetMiddleware",
    ]
    loop_names = [
        "Agent",
        "AgentConversation",
        "AgentRuntimeState",
        "TaskBoard",
        "TraceWriter",
        "llm_call_simple",
    ]

    assert [name for name in tool_names if not hasattr(tools, name)] == []
    assert [name for name in middleware_names if not hasattr(middlewares, name)] == []
    assert [name for name in loop_names if not hasattr(loop, name)] == []
