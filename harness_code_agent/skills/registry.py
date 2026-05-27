"""
Skill registry — implements Anthropic's three-level progressive disclosure.

Level 1: At startup, scan skills/ directory, load ONLY name + description
         from YAML frontmatter. Inject this catalog into system prompts.
Level 2: Agent decides a skill is relevant → reads SKILL.md via read_file tool.
Level 3: SKILL.md references sub-files → Agent reads those on demand too.

The key insight: the AGENT decides when to load skills, not external code.
We just make the catalog visible and the files accessible.

Structure:
  skills/
    frontend-design/
      SKILL.md          ← frontmatter (name, description) + instructions
      reference.md      ← additional detail, referenced from SKILL.md
    another-skill/
      SKILL.md
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..planning_policy import PLANNING_MODE_CATALOG_POLICY

log = logging.getLogger("harness")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "skills"


class SkillRegistry:
    """
    Discovers skills at startup (metadata only).
    Provides a catalog string for injection into system prompts.
    Does NOT load or inject skill content — that's the agent's job.
    """

    def __init__(self, skills_dir: Path | str | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        self.catalog: list[dict[str, str]] = []
        self._discover()

    def _discover(self):
        """Scan skills directory, load only metadata (name + description)."""
        if not self.skills_dir.is_dir():
            log.info(f"No skills directory found at {self.skills_dir}")
            return

        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            meta = _parse_frontmatter(skill_file)
            if meta:
                name = meta.get("name", skill_file.parent.name)
                desc = meta.get("description", "")
                rel_path = skill_file.relative_to(PROJECT_ROOT)
                self.catalog.append({
                    "name": name,
                    "description": desc,
                    "path": str(rel_path),
                })
                log.info(f"Discovered skill: {name} — {desc[:80]}")

    def build_catalog_prompt(self) -> str:
        """
        Build the catalog string to inject into system prompts.
        This is Level 1 of progressive disclosure — just metadata.
        The agent sees what skills exist and can choose to load them.
        """
        if not self.catalog:
            return ""

        lines = [
            "\n## Available Skills",
            "The following skills are available. If a skill is relevant to your "
            "current task, load it by reading its SKILL.md file with the read_skill_file tool. "
            "Only load skills you actually need — don't load them all.\n",
            "Skill routing policy:",
            "- If PRD.md exists in the workspace, read it first and use it as the product requirements source of truth.",
            "- If the task starts a new or fuzzy project/major feature, read `skills/prd/SKILL.md` before implementation and create or update PRD.md as the requirements artifact.",
            "- If the task is already scoped by PRD.md or the user request, do not re-run PRD planning; execute directly from the existing context.",
            "- Treat `prd` and runtime planning state as collaborators: PRD.md defines goal, scope, non-goals, acceptance criteria, first slice, and risks; update_plan_state tracks light/full execution.",
            f"- {PLANNING_MODE_CATALOG_POLICY}",
            "- If execution touches high-risk or tightly bounded areas, read `skills/vibe-execution-guard/SKILL.md` before editing.",
            "- Keep PRD.md current when scope, requirements, acceptance criteria, risks, or major product decisions change.\n",
        ]
        for skill in self.catalog:
            lines.append(
                f"- **{skill['name']}**: {skill['description']}\n"
                f"  Path: `{skill['path']}`"
            )
        return "\n".join(lines) + "\n"


def _parse_frontmatter(path: Path) -> dict | None:
    """Parse YAML frontmatter from a Markdown file."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = _strip_scalar_quotes(value.strip())
    return meta


def _strip_scalar_quotes(value: str) -> str:
    """Remove one pair of matching YAML-style scalar quotes for catalog text."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
