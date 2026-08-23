from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

INTENT_KEYWORDS = {
    "debug": ("debug", "bug", "error", "exception", "failure", "失败", "报错", "调试"),
    "test": ("test", "unittest", "assert", "测试", "用例"),
    "architecture": ("architecture", "design", "module", "架构", "设计", "模块", "方案"),
    "workflow": ("workflow", "process", "步骤", "流程"),
    "preference": ("preference", "prefer", "偏好", "习惯", "要求"),
    "command": ("command", "shell", "powershell", "命令"),
}


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    terms: list[str]
    intents: list[str]

    @property
    def should_recall(self) -> bool:
        return bool(self.text.strip())


class MemoryQueryComposer:
    def compose(
        self,
        user_turn: str,
        *,
        mentions: list[str] | None = None,
    ) -> MemoryQuery:
        parts: list[str] = []
        parts.extend(_keywords(user_turn))
        for mention in mentions or []:
            parts.extend(_path_terms(mention))
        intents = _intent_labels(user_turn)
        parts.extend(intents)
        terms = _dedupe(parts)
        return MemoryQuery(text=" ".join(terms), terms=terms, intents=intents)


def _keywords(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}", text.lower())
    return [token for token in tokens if len(token) > 1][:24]


def _path_terms(path: str) -> list[str]:
    p = Path(path)
    terms = [part for part in p.parts[-4:] if part not in {".", ""}]
    if p.suffix:
        terms.append(p.suffix.lstrip("."))
    return terms


def _intent_labels(text: str) -> list[str]:
    lowered = text.lower()
    labels = []
    for label, keywords in INTENT_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            labels.append(label)
    return labels


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
