# Profile registry

from .app_builder import AppBuilderProfile
from .base import BaseProfile, ProfileConfig
from .coding_agent import CodingAgentProfile
from .general import GeneralProfile
from .plan import PlanProfile
from .review import ReviewProfile
from .terminal import TerminalProfile

PROFILES: dict[str, type[BaseProfile]] = {
    "general": GeneralProfile,
    "coding-agent": CodingAgentProfile,
    "app-builder": AppBuilderProfile,
    "terminal": TerminalProfile,
    "plan": PlanProfile,
    "review": ReviewProfile,
}

PRODUCT_PROFILES: dict[str, type[BaseProfile]] = {
    name: cls
    for name, cls in PROFILES.items()
    if name != "terminal"
}


def get_profile(name: str, cfg: ProfileConfig | None = None) -> BaseProfile:
    """Get a profile instance by name, optionally with custom config."""
    cls = PROFILES.get(name)
    if cls is None:
        available = ", ".join(PROFILES.keys())
        raise ValueError(f"Unknown profile: {name}. Available: {available}")
    return cls(cfg=cfg)


def list_profiles() -> list[dict[str, str]]:
    """List product-visible profiles."""
    return [
        {"name": cls().name(), "description": cls().description()}
        for cls in PRODUCT_PROFILES.values()
    ]
