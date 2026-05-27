"""Prompt prefix construction for stable cache-friendly agent context."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


MAIN_AGENT_OWNERSHIP_RULES = """\
- Only the main agent may modify files, create tests, integrate results, and decide when to stop.
- Consultation sub-agents are read-only and may only return findings, evidence, recommendations, and risks.
- Verify the acceptance criteria against actual files or command output before stopping.
"""


@dataclass(frozen=True)
class GlobalRulesDoc:
    source: str
    content: str

    @property
    def content_hash(self) -> str:
        return _hash_text(self.content)


@dataclass(frozen=True)
class StablePromptPrefix:
    content: str
    hashes: dict[str, str]

    @property
    def cache_identity(self) -> dict[str, str]:
        return dict(self.hashes)


class PromptPrefixBuilder:
    """Builds the stable system prefix in a deterministic section order."""

    def build(
        self,
        *,
        profile_prompt: str,
        global_rules_docs: list[GlobalRulesDoc] | None = None,
        skill_catalog: str = "",
        acceptance_criteria: list[str] | None = None,
    ) -> StablePromptPrefix:
        global_rules_docs = global_rules_docs or []
        criteria = acceptance_criteria or []

        global_rules_content = "\n\n".join(
            _global_rules_section(doc) for doc in global_rules_docs if doc.content.strip()
        )
        criteria_content = "\n".join(f"- {item}" for item in criteria) or "- Verify the task requirements before stopping."

        sections = [
            ("Harness Agent Contract", profile_prompt.strip()),
        ]
        if global_rules_content:
            sections.append(("Global Rules Bundle", global_rules_content))
        sections.extend(
            [
                ("Profile Acceptance Criteria", criteria_content),
                ("Main-Agent Ownership Rules", MAIN_AGENT_OWNERSHIP_RULES.strip()),
            ]
        )
        if skill_catalog.strip():
            sections.append(("Stable Skill Catalog", skill_catalog.strip()))

        content = "\n\n".join(f"## {title}\n{body}" for title, body in sections if body)
        global_hash_payload = "\n\n".join(
            f"{doc.source}\n{doc.content}" for doc in global_rules_docs
        )
        hashes = {
            "profile_prompt_hash": _hash_text(profile_prompt),
            "global_rules_hash": _hash_text(global_hash_payload),
            "skill_catalog_hash": _hash_text(skill_catalog),
            "acceptance_criteria_hash": _hash_text(criteria_content),
            "stable_prefix_hash": _hash_text(content),
        }
        return StablePromptPrefix(content=content, hashes=hashes)


def _global_rules_section(doc: GlobalRulesDoc) -> str:
    name = doc.source.replace("\\", "/").rstrip("/").split("/")[-1] or doc.source
    return (
        f"### {name}\n"
        f"source: {doc.source}\n"
        f"hash: {doc.content_hash}\n"
        "content:\n"
        f"{doc.content.strip()}"
    )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
