"""Skill discovery and invocation with progressive disclosure.

Model-invoked skills expose only their descriptions in the stable prompt.
User-invoked skills are absent from that prompt and run only through `/name`.
Both kinds keep their full instructions on disk until a turn needs them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..tracking_policy import TASK_TRACKING_CATALOG_POLICY

log = logging.getLogger("harness")

SKILLS_DIR = Path(__file__).resolve().parent / "catalog"


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: str
    source_path: Path
    disable_model_invocation: bool = False
    argument_hint: str = ""

    def as_catalog_item(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "disable_model_invocation": self.disable_model_invocation,
            "argument_hint": self.argument_hint,
        }


@dataclass(frozen=True)
class SkillInvocation:
    name: str
    arguments: str
    path: str
    prompt: str


class SkillRegistry:
    """Discover skills and expose separate model and user invocation surfaces."""

    def __init__(self, skills_dir: Path | str | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        self.skills: list[SkillMetadata] = []
        self._by_name: dict[str, SkillMetadata] = {}
        self._discover()

    @property
    def model_catalog(self) -> list[dict[str, str | bool]]:
        return [
            skill.as_catalog_item()
            for skill in self.skills
            if not skill.disable_model_invocation
        ]

    @property
    def user_commands(self) -> list[dict[str, str | bool]]:
        return [
            skill.as_catalog_item()
            for skill in self.skills
            if skill.disable_model_invocation
        ]

    def _discover(self) -> None:
        if not self.skills_dir.is_dir():
            log.info("No skills directory found at %s", self.skills_dir)
            return

        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            meta = _parse_frontmatter(skill_file)
            if meta is None:
                raise ValueError(f"Skill is missing valid frontmatter: {skill_file}")
            name = str(meta.get("name", "")).strip()
            if not name:
                raise ValueError(f"Skill has no name: {skill_file}")
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                raise ValueError(f"Invalid skill name '{name}' in {skill_file}")
            if skill_file.parent.name != name:
                raise ValueError(
                    f"Skill name '{name}' must match folder '{skill_file.parent.name}'"
                )
            description = str(meta.get("description", "")).strip()
            if not description:
                raise ValueError(f"Skill '{name}' has no description: {skill_file}")
            key = name.lower()
            if key in self._by_name:
                previous = self._by_name[key]
                raise ValueError(
                    f"Duplicate skill name '{name}': {previous.path} and {skill_file}"
                )
            relative = Path(self.skills_dir.name) / skill_file.relative_to(self.skills_dir)
            skill = SkillMetadata(
                name=name,
                description=description,
                path=relative.as_posix(),
                source_path=skill_file,
                disable_model_invocation=_parse_bool(
                    meta.get("disable-model-invocation", False)
                ),
                argument_hint=str(meta.get("argument-hint", "")).strip(),
            )
            self.skills.append(skill)
            self._by_name[key] = skill
            log.debug("Discovered skill: %s", name)

    def build_catalog_prompt(self) -> str:
        """Build the stable prompt catalog for model-invoked skills only."""
        catalog = self.model_catalog
        if not catalog:
            return ""

        lines = [
            "\n## Available Skills",
            ("The following model-invoked skills are available. If one is relevant, "
            "load its SKILL.md with read_skill_file. User-invoked `/name` skills are "
            "intentionally absent from this catalog.\n"),
            "Skill routing policy:",
            "- Load only the most specific relevant skill; do not expand a scoped task into a larger workflow.",
            "- Explicit user instructions and repository rules override skill suggestions.",
            "- Existing specs, tickets, ADRs, and plans are evidence to read when relevant, not mandatory artifacts for every task.",
            f"- {TASK_TRACKING_CATALOG_POLICY}",
            "- If execution touches high-risk or tightly bounded areas, read `catalog/vibe-execution-guard/SKILL.md` before editing.",
            "- User-invoked workflows remain opt-in and run only through their explicit `/name` command.\n",
        ]
        for skill in catalog:
            lines.append(
                f"- **{skill['name']}**: {skill['description']}\n"
                f"  Path: `{skill['path']}`"
            )
        return "\n".join(lines) + "\n"

    def build_user_invocation(self, line: str) -> SkillInvocation | None:
        """Expand an exact user skill command into an agent-facing instruction."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            return None
        command, separator, arguments = stripped.partition(" ")
        name = command.removeprefix("/").strip().lower()
        skill = self._by_name.get(name)
        if skill is None or not skill.disable_model_invocation:
            return None
        body = skill.source_path.read_text(encoding="utf-8", errors="replace")
        arguments = arguments.strip() if separator else ""
        prompt = "\n".join(
            [
                f"User explicitly invoked `/{skill.name}`.",
                f"Skill source: `{skill.path}`",
                f"Arguments: {arguments or '(none)'}",
                ("Follow the skill instructions below for this turn. "
                "Do not treat the frontmatter description as model-routing guidance."),
                "",
                "<invoked-skill>",
                body,
                "</invoked-skill>",
            ]
        )
        return SkillInvocation(
            name=skill.name,
            arguments=arguments,
            path=skill.path,
            prompt=prompt,
        )


def _parse_frontmatter(path: Path) -> dict[str, str] | None:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in meta:
            raise ValueError(f"Duplicate frontmatter key '{key}' in {path}")
        meta[key] = _strip_scalar_quotes(_strip_yaml_comment(value.strip()))
    return meta


def _strip_scalar_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_yaml_comment(value: str) -> str:
    """Strip a trailing YAML inline comment, respecting quoted strings."""
    in_quote = False
    quote_char: str | None = None
    for i, ch in enumerate(value):
        if ch in ("'", '"') and (i == 0 or value[i - 1] != "\\"):
            if not in_quote:
                in_quote = True
                quote_char = ch
            elif ch == quote_char:
                in_quote = False
        elif ch == "#" and not in_quote:
            return value[:i].rstrip()
    return value


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1", "on"}:
        return True
    if normalized in {"false", "no", "0", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean frontmatter value: {value}")
