"""Tool registry and schema filtering primitives."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .execution_planner import CallEffect
from .permissions import VALID_TOOL_PERMISSIONS

if TYPE_CHECKING:
    from .tool_context import ToolContext

EffectResolver = Callable[[dict, "ToolContext | None"], CallEffect]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    handler: Callable
    permission: str
    effect_resolver: EffectResolver
    disclosure: str = "core"


VALID_TOOL_DISCLOSURES = {"core", "deferred"}


class ToolRegistry:
    """Thin registry boundary for built-in and future profile-provided tools."""

    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}
        self._permissions: dict[str, str] = {}
        self._effects: dict[str, EffectResolver] = {}
        self._disclosures: dict[str, str] = {}

    def register(
        self,
        schema: dict,
        handler: Callable,
        *,
        permission: str | None = None,
        effect: EffectResolver | CallEffect | None = None,
        disclosure: str = "core",
    ) -> None:
        name = schema.get("function", {}).get("name")
        if not name:
            raise ValueError("Tool schema missing function.name")
        if permission is None:
            raise ValueError(f"Tool {name} missing permission classification")
        if permission not in VALID_TOOL_PERMISSIONS:
            raise ValueError(f"Tool {name} has unknown permission classification: {permission}")
        if disclosure not in VALID_TOOL_DISCLOSURES:
            raise ValueError(f"Tool {name} has unknown disclosure classification: {disclosure}")
        self._schemas[name] = schema
        self._handlers[name] = handler
        self._permissions[name] = permission
        if isinstance(effect, CallEffect):
            self._effects[name] = lambda _args, _context, value=effect: value
        elif effect is not None:
            self._effects[name] = effect
        else:
            self._effects[name] = lambda _args, _context: CallEffect.global_exclusive()
        self._disclosures[name] = disclosure

    def get(self, name: str) -> Callable | None:
        return self._handlers.get(name)

    def permission_for(self, name: str) -> str | None:
        return self._permissions.get(name)

    def effect_for(self, name: str, args: dict, context: ToolContext | None = None) -> CallEffect:
        resolver = self._effects.get(name)
        return resolver(dict(args or {}), context) if resolver is not None else CallEffect.global_exclusive()

    def effect_resolver_for(self, name: str) -> EffectResolver | None:
        return self._effects.get(name)

    def disclosure_for(self, name: str) -> str | None:
        return self._disclosures.get(name)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name,
                self._schemas[name],
                self._handlers[name],
                self._permissions[name],
                self._effects[name],
                self._disclosures.get(name, "core"),
            )
            for name in sorted(self._schemas)
        ]

    def schemas(self) -> list[dict]:
        return [self._schemas[name] for name in sorted(self._schemas)]

    def dispatch(self) -> dict[str, Callable]:
        return dict(self._handlers)

    def copy(self) -> ToolRegistry:
        clone = ToolRegistry()
        clone._schemas = dict(self._schemas)
        clone._handlers = dict(self._handlers)
        clone._permissions = dict(self._permissions)
        clone._effects = dict(self._effects)
        clone._disclosures = dict(self._disclosures)
        return clone


def tool_schemas_for_profile(
    *,
    allowed_permissions: set[str] | None = None,
    include_names: set[str] | None = None,
    exclude_names: set[str] | None = None,
    registry: ToolRegistry | None = None,
    disclosure: set[str] | None = None,
) -> list[dict]:
    if registry is None:
        from .builtins.registry import BUILTIN_TOOL_REGISTRY

        registry = BUILTIN_TOOL_REGISTRY
    allowed_permissions = set(allowed_permissions) if allowed_permissions is not None else None
    include_names = set(include_names or set())
    exclude_names = set(exclude_names or set())
    disclosures = {"core"} if disclosure is None else set(disclosure)
    if allowed_permissions is not None:
        unknown_permissions = allowed_permissions - VALID_TOOL_PERMISSIONS
        if unknown_permissions:
            names = ", ".join(sorted(unknown_permissions))
            raise ValueError(f"Unknown tool permission classification for profile: {names}")
    unknown_disclosures = disclosures - VALID_TOOL_DISCLOSURES
    if unknown_disclosures:
        names = ", ".join(sorted(unknown_disclosures))
        raise ValueError(f"Unknown tool disclosure classification for profile: {names}")

    schemas: list[dict] = []
    for spec in registry.specs():
        if spec.name in exclude_names:
            continue
        if spec.disclosure not in disclosures:
            continue
        if spec.name in include_names or (
            allowed_permissions is not None and spec.permission in allowed_permissions
        ):
            schemas.append(spec.schema)
    return schemas
