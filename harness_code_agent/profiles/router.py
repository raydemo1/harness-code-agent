"""Local conservative profile routing."""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache


DEFAULT_PROFILE = "general"
LOCAL_ROUTE_MIN_CONFIDENCE = 0.10
LOCAL_ROUTE_MIN_MARGIN = 0.035
LOCAL_ROUTE_PROFILES = {"general", "coding-agent", "review", "plan", "app-builder"}
ROUTE_ACTION_STAY = "stay"
ROUTE_ACTION_SWITCH_PROFILE = "switch_profile"
ROUTE_ACTION_DIRECT_ANSWER = "direct_answer"
TURN_MODE_NORMAL = "normal"
TURN_MODE_DIRECT_ANSWER = "direct_answer"


@dataclass(frozen=True)
class RouteDecision:
    profile_name: str
    confidence: float
    reason: str
    fallback_used: bool = False
    fallback_reason: str = ""
    elapsed_ms: float = 0.0
    margin: float = 0.0
    source: str = "local"
    action: str = ROUTE_ACTION_STAY
    turn_mode: str = TURN_MODE_NORMAL
    matched_profile: str = ""


def route_profile_for_turn(
    user_prompt: str,
    *,
    current_profile: str,
    confidence_threshold: float = LOCAL_ROUTE_MIN_CONFIDENCE,
    margin_threshold: float = LOCAL_ROUTE_MIN_MARGIN,
) -> RouteDecision:
    """Conservatively route a turn using local semantic prototypes only."""
    started_at = time.perf_counter()
    current = current_profile if current_profile in LOCAL_ROUTE_PROFILES else current_profile
    if current not in LOCAL_ROUTE_PROFILES:
        return _with_elapsed(
            started_at,
            RouteDecision(
                profile_name=current,
                confidence=0.0,
                reason="Current profile is outside product auto-route candidates.",
                fallback_used=True,
                fallback_reason="profile is sticky",
                source="local",
                matched_profile=current,
            ),
        )

    scores = _local_route_scores(user_prompt)
    if not scores:
        return _with_elapsed(started_at, _local_fallback(current, "empty local route query"))

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_profile, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = max(0.0, best_score - second_score)

    if best_score < confidence_threshold or margin < margin_threshold:
        decision = _local_fallback(current, "low local route confidence")
        return _with_elapsed(started_at, _replace_route_scores(decision, best_score, margin))

    if current == "general" and best_profile != "general":
        return _with_elapsed(
            started_at,
            RouteDecision(
                profile_name=best_profile,
                confidence=best_score,
                margin=margin,
                reason=f"Local semantic prototype matched {best_profile}.",
                source="local",
                action=ROUTE_ACTION_SWITCH_PROFILE,
                matched_profile=best_profile,
            ),
        )

    if current != "general" and best_profile == "general":
        return _with_elapsed(
            started_at,
            RouteDecision(
                profile_name=current,
                confidence=best_score,
                margin=margin,
                reason="Local semantic prototype matched general; answer directly in the current profile.",
                source="local",
                action=ROUTE_ACTION_DIRECT_ANSWER,
                turn_mode=TURN_MODE_DIRECT_ANSWER,
                matched_profile=best_profile,
            ),
        )

    if current != "general" and best_profile != current:
        decision = _local_fallback(current, f"specialized profile sticky; local best was {best_profile}")
        return _with_elapsed(started_at, _replace_route_scores(decision, best_score, margin))

    return _with_elapsed(
        started_at,
        RouteDecision(
            profile_name=best_profile,
            confidence=best_score,
            margin=margin,
            reason=f"Local semantic prototype matched {best_profile}.",
            source="local",
            action=ROUTE_ACTION_STAY,
            matched_profile=best_profile,
        ),
    )


def _local_fallback(profile_name: str, fallback_reason: str) -> RouteDecision:
    return RouteDecision(
        profile_name=profile_name,
        confidence=0.0,
        reason="Keeping the current profile.",
        fallback_used=True,
        fallback_reason=fallback_reason,
        source="local",
        action=ROUTE_ACTION_STAY,
        matched_profile=profile_name,
    )


def _replace_route_scores(decision: RouteDecision, confidence: float, margin: float) -> RouteDecision:
    return RouteDecision(
        profile_name=decision.profile_name,
        confidence=confidence,
        margin=margin,
        reason=decision.reason,
        fallback_used=decision.fallback_used,
        fallback_reason=decision.fallback_reason,
        source=decision.source,
        action=decision.action,
        turn_mode=decision.turn_mode,
        matched_profile=decision.matched_profile,
    )


def _with_elapsed(started_at: float, decision: RouteDecision) -> RouteDecision:
    return RouteDecision(
        profile_name=decision.profile_name,
        confidence=decision.confidence,
        reason=decision.reason,
        fallback_used=decision.fallback_used,
        fallback_reason=decision.fallback_reason,
        elapsed_ms=(time.perf_counter() - started_at) * 1000,
        margin=decision.margin,
        source=decision.source,
        action=decision.action,
        turn_mode=decision.turn_mode,
        matched_profile=decision.matched_profile,
    )


def _local_route_scores(user_prompt: str) -> dict[str, float]:
    query = _text_vector(user_prompt)
    if not query:
        return {}
    return {
        profile: _cosine_similarity(query, vector)
        for profile, vector in _prototype_vectors().items()
    }


@lru_cache(maxsize=1)
def _prototype_vectors() -> dict[str, Counter[str]]:
    return {
        profile: _text_vector(text)
        for profile, text in _LOCAL_ROUTE_PROTOTYPES.items()
    }


def _text_vector(text: str) -> Counter[str]:
    value = str(text or "").lower()
    vector: Counter[str] = Counter()
    for token in re.findall(r"[a-z0-9_./:-]+", value):
        if len(token) > 1:
            vector[token] += 1.0
        for part in re.split(r"[^a-z0-9]+", token):
            if len(part) > 2:
                vector[part] += 0.5
    for sequence in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(sequence) == 1:
            vector[sequence] += 0.2
            continue
        for ngram_size in (2, 3):
            if len(sequence) < ngram_size:
                continue
            for index in range(0, len(sequence) - ngram_size + 1):
                vector[sequence[index:index + ngram_size]] += 1.0
    return vector


def _cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    if dot <= 0:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


_LOCAL_ROUTE_PROTOTYPES = {
    "general": """
        who are you what can you do explain this concept answer a question summarize discuss compare
        help me understand brainstorm talk through analyze this screenshot image product idea
        你是谁 你能做什么 解释一下 说明一下 总结一下 讨论一下 分析这张图 帮我理解 普通问题
        先聊聊 看看这个想法 回答即可
    """,
    "coding-agent": """
        implement fix change update modify refactor migrate add feature edit files write code
        run tests verify failing test bug regression make the code pass patch repository
        修复 修改 实现 改代码 调整 迁移 加功能 跑测试 验证 落地 修 bug 提交补丁
    """,
    "review": """
        review code review inspect changes find issues read-only assessment critique audit pull request diff
        findings severity risk regression missing tests security correctness maintainability
        审查 评审 review 看代码有没有问题 检查变更 找问题 只读评估 风险 严重程度
    """,
    "plan": """
        plan design proposal implementation plan architecture plan do not implement investigate and produce plan
        steps roadmap approach tradeoffs assumptions test plan decision complete
        计划 方案 设计方案 实施方案 实现方案 不要实现 不要改代码 先规划 怎么做 路线 取舍 风险 假设
    """,
    "app-builder": """
        build app create website web app page UI frontend component browser responsive design todo
        html css javascript react vite interface polished app game dashboard landing page
        做一个网页 创建应用 前端 UI 页面 浏览器 响应式 组件 看板 小游戏 可运行界面 todo
    """,
}
