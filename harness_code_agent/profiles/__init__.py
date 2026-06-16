# Profile registry

from .base import BaseProfile, AgentConfig, ProfileConfig
from .app_builder import AppBuilderProfile
from .basic_eval import BasicEvalProfile
from .coding_agent import CodingAgentProfile
from .plan import PlanProfile
from .review import ReviewProfile
from .terminal import TerminalProfile
from .swe_bench import SWEBenchProfile

PROFILES: dict[str, type[BaseProfile]] = {
    "coding-agent": CodingAgentProfile,
    "basic-eval": BasicEvalProfile,
    "app-builder": AppBuilderProfile,
    "terminal": TerminalProfile,
    "swe-bench": SWEBenchProfile,
    "plan": PlanProfile,
    "review": ReviewProfile,
}


def get_profile(name: str, cfg: ProfileConfig | None = None) -> BaseProfile:
    """Get a profile instance by name, optionally with custom config."""
    cls = PROFILES.get(name)
    if cls is None:
        available = ", ".join(PROFILES.keys())
        raise ValueError(f"Unknown profile: {name}. Available: {available}")
    return cls(cfg=cfg)


def list_profiles() -> list[dict[str, str]]:
    """List all available profiles."""
    return [
        {"name": cls().name(), "description": cls().description()}
        for cls in PROFILES.values()
    ]
