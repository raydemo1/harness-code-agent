"""Conservative hybrid profile routing."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

DEFAULT_PROFILE = "general"
ROUTING_MODE_AUTO = "auto"
ROUTING_MODE_PINNED = "pinned"
LOCAL_ROUTE_MIN_CONFIDENCE = 0.72
LOCAL_ROUTE_MIN_MARGIN = 0.18
LLM_ROUTE_MIN_CONFIDENCE = 0.80
LLM_ROUTE_TIMEOUT_SECONDS = 3.0
LOCAL_ROUTE_BM25_K1 = 1.2
LOCAL_ROUTE_BM25_B = 0.75
LOCAL_ROUTE_PROFILES = {"general", "coding-agent", "review", "plan", "app-builder"}
ROUTE_ACTION_STAY = "stay"
ROUTE_ACTION_SWITCH_PROFILE = "switch_profile"
ROUTE_ACTION_DIRECT_ANSWER = "direct_answer"
TURN_MODE_NORMAL = "normal"
TURN_MODE_DIRECT_ANSWER = "direct_answer"

log = logging.getLogger("harness")

_LLM_ROUTE_SYSTEM_PROMPT = """You route user turns between five VeriForge workflow profiles.
Return one JSON object only: {"profile": string, "confidence": number, "reason": string}.
The profile must be exactly one of: general, coding-agent, review, plan, app-builder.

Choose by the final deliverable, not isolated keywords:
- general: explanations, discussion, summaries, and questions needing no workspace action.
- coding-agent: modify, fix, refactor, test, or otherwise work in an existing repository. Review-then-fix also belongs here. Existing UI changes belong here.
- review: an explicit code-review workflow for source code, a PR, diff, patch, or implementation. Use it only when the user clearly asks to review, audit, or inspect code and wants findings. Do not choose review for an ordinary question about the current implementation, and do not choose it when the user also asks to fix findings.
- plan: investigate and produce a decision-complete plan without implementation. Explicit no-edit or wait-for-approval constraints belong here unless the user only wants an explanation.
- app-builder: create a complete new browser application or product interface from an idea. Do not choose it merely because an existing repository uses React or has a UI.

Use the previous exchange to resolve follow-ups such as "continue" or "implement that plan". Summaries, explanations, and brief questions about the work already in progress should keep the current specialized profile. Switch between specialized profiles only for a clear workflow phase change. If genuinely ambiguous, keep the current profile and lower confidence. Do not invent a sixth route."""


@dataclass(frozen=True)
class LlmRouteResult:
    profile_name: str = ""
    confidence: float = 0.0
    reason: str = ""
    provider: str = ""
    model: str = ""
    failure_type: str = ""


RouteClassifier = Callable[..., LlmRouteResult]


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
    routing_mode: str = ROUTING_MODE_AUTO
    decisive_signal: str = ""
    local_candidate: str = ""
    local_confidence: float = 0.0
    local_margin: float = 0.0
    llm_called: bool = False
    llm_confidence: float = 0.0
    llm_provider: str = ""
    llm_model: str = ""
    failure_type: str = ""


def route_profile_for_turn(
    user_prompt: str,
    *,
    current_profile: str,
    routing_mode: str = ROUTING_MODE_AUTO,
    confidence_threshold: float = LOCAL_ROUTE_MIN_CONFIDENCE,
    margin_threshold: float = LOCAL_ROUTE_MIN_MARGIN,
    previous_user_task: str = "",
    previous_assistant_text: str = "",
    llm_classifier: RouteClassifier | None = None,
) -> RouteDecision:
    """Route one turn through explicit, local, then fast-LLM decisions."""
    started_at = time.perf_counter()
    current = current_profile
    mode = routing_mode if routing_mode in {ROUTING_MODE_AUTO, ROUTING_MODE_PINNED} else ROUTING_MODE_AUTO
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
                routing_mode=mode,
            ),
        )

    if mode == ROUTING_MODE_PINNED:
        return _with_elapsed(
            started_at,
            RouteDecision(
                profile_name=current,
                confidence=1.0,
                margin=1.0,
                reason="Profile is pinned by the user.",
                source="pinned",
                action=ROUTE_ACTION_STAY,
                matched_profile=current,
                routing_mode=mode,
                decisive_signal="pinned",
            ),
        )

    explicit_mode = _explicit_mode_profile(user_prompt)
    if explicit_mode is not None:
        return _with_elapsed(started_at, _decision_for_candidate(
            current=current,
            target=explicit_mode,
            confidence=1.0,
            margin=1.0,
            reason=f"Explicit mode selection matched {explicit_mode}.",
            source="local",
            routing_mode=mode,
            decisive_signal="explicit_mode",
            force_general_switch=True,
        ))

    local_contract = _explicit_route_profile(user_prompt)
    scores = _local_route_scores(user_prompt)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    local_candidate = ranked[0][0] if ranked else ""
    local_confidence = ranked[0][1] if ranked else 0.0
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    local_margin = max(0.0, local_confidence - second_score)

    if local_contract is not None:
        return _with_elapsed(started_at, _decision_for_candidate(
            current=current,
            target=local_contract,
            confidence=0.98,
            margin=1.0,
            reason=f"High-precision local contract matched {local_contract}.",
            source="local",
            routing_mode=mode,
            decisive_signal="local_contract",
            local_candidate=local_candidate,
            local_confidence=local_confidence,
            local_margin=local_margin,
        ))

    if (
        local_candidate
        and local_confidence >= confidence_threshold
        and local_margin >= margin_threshold
        and (
            current == "general"
            or local_candidate == current
            or local_candidate == "general"
        )
    ):
        return _with_elapsed(started_at, _decision_for_candidate(
            current=current,
            target=local_candidate,
            confidence=local_confidence,
            margin=local_margin,
            reason=f"High-confidence local prototype matched {local_candidate}.",
            source="local",
            routing_mode=mode,
            decisive_signal="local_semantic",
            local_candidate=local_candidate,
            local_confidence=local_confidence,
            local_margin=local_margin,
        ))

    classifier = llm_classifier or _classify_with_fast_llm
    try:
        llm_result = classifier(
            user_prompt=user_prompt,
            current_profile=current,
            previous_user_task=previous_user_task,
            previous_assistant_text=previous_assistant_text,
        )
    except Exception as exc:  # noqa: BLE001 - routing failures must degrade to stay
        log.info("Fast profile router failed: %s", exc)
        llm_result = LlmRouteResult(failure_type=_failure_type_for_exception(exc))
    if not isinstance(llm_result, LlmRouteResult):
        llm_result = LlmRouteResult(failure_type="invalid_response")

    if (
        llm_result.failure_type
        or llm_result.profile_name not in LOCAL_ROUTE_PROFILES
        or llm_result.confidence < LLM_ROUTE_MIN_CONFIDENCE
    ):
        failure_type = llm_result.failure_type
        if not failure_type:
            failure_type = "low_confidence" if llm_result.profile_name in LOCAL_ROUTE_PROFILES else "invalid_response"
        return _with_elapsed(started_at, RouteDecision(
            profile_name=current,
            confidence=llm_result.confidence,
            reason="Keeping the current profile because the model router was not decisive.",
            fallback_used=True,
            fallback_reason=f"llm router {failure_type}",
            source="llm",
            action=ROUTE_ACTION_STAY,
            matched_profile=llm_result.profile_name or current,
            routing_mode=mode,
            decisive_signal="llm_fallback",
            local_candidate=local_candidate,
            local_confidence=local_confidence,
            local_margin=local_margin,
            llm_called=True,
            llm_confidence=llm_result.confidence,
            llm_provider=llm_result.provider,
            llm_model=llm_result.model,
            failure_type=failure_type,
        ))

    return _with_elapsed(started_at, _decision_for_candidate(
        current=current,
        target=llm_result.profile_name,
        confidence=llm_result.confidence,
        margin=0.0,
        reason=llm_result.reason or f"Fast model selected {llm_result.profile_name}.",
        source="llm",
        routing_mode=mode,
        decisive_signal="llm",
        local_candidate=local_candidate,
        local_confidence=local_confidence,
        local_margin=local_margin,
        llm_called=True,
        llm_confidence=llm_result.confidence,
        llm_provider=llm_result.provider,
        llm_model=llm_result.model,
    ))


def _decision_for_candidate(
    *,
    current: str,
    target: str,
    confidence: float,
    margin: float,
    reason: str,
    source: str,
    routing_mode: str,
    decisive_signal: str,
    force_general_switch: bool = False,
    local_candidate: str = "",
    local_confidence: float = 0.0,
    local_margin: float = 0.0,
    llm_called: bool = False,
    llm_confidence: float = 0.0,
    llm_provider: str = "",
    llm_model: str = "",
) -> RouteDecision:
    profile_name = target
    action = ROUTE_ACTION_STAY
    turn_mode = TURN_MODE_NORMAL
    if target == "general" and current != "general" and not force_general_switch:
        profile_name = current
        action = ROUTE_ACTION_DIRECT_ANSWER
        turn_mode = TURN_MODE_DIRECT_ANSWER
    elif target != current:
        action = ROUTE_ACTION_SWITCH_PROFILE
    return RouteDecision(
        profile_name=profile_name,
        confidence=confidence,
        margin=margin,
        reason=reason,
        source=source,
        action=action,
        turn_mode=turn_mode,
        matched_profile=target,
        routing_mode=routing_mode,
        decisive_signal=decisive_signal,
        local_candidate=local_candidate,
        local_confidence=local_confidence,
        local_margin=local_margin,
        llm_called=llm_called,
        llm_confidence=llm_confidence,
        llm_provider=llm_provider,
        llm_model=llm_model,
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
        routing_mode=decision.routing_mode,
        decisive_signal=decision.decisive_signal,
        local_candidate=decision.local_candidate,
        local_confidence=decision.local_confidence,
        local_margin=decision.local_margin,
        llm_called=decision.llm_called,
        llm_confidence=decision.llm_confidence,
        llm_provider=decision.llm_provider,
        llm_model=decision.llm_model,
        failure_type=decision.failure_type,
    )


def _classify_with_fast_llm(
    *,
    user_prompt: str,
    current_profile: str,
    previous_user_task: str = "",
    previous_assistant_text: str = "",
) -> LlmRouteResult:
    from .. import config
    from ..agent.providers import ProviderAdapter, get_client

    profile = config.resolve_model_profile("fast")
    adapter = ProviderAdapter(profile.provider)
    client = get_client().with_options(timeout=LLM_ROUTE_TIMEOUT_SECONDS, max_retries=0)
    request = {
        "current_profile": current_profile,
        "previous_user_task": _truncate_route_context(previous_user_task, 800),
        "previous_assistant_answer": _truncate_route_context(previous_assistant_text, 1200),
        "user_request": str(user_prompt or ""),
    }
    try:
        response = client.chat.completions.create(**adapter.chat_kwargs(
            profile=profile,
            messages=[
                {"role": "system", "content": _LLM_ROUTE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            max_tokens=160,
        ))
        raw = response.choices[0].message.content or ""
        parsed = _parse_llm_route_result(raw)
        return LlmRouteResult(
            profile_name=parsed.profile_name,
            confidence=parsed.confidence,
            reason=parsed.reason,
            provider=profile.provider,
            model=profile.model,
            failure_type=parsed.failure_type,
        )
    except Exception as exc:  # noqa: BLE001 - provider failures must degrade to stay
        log.info("Fast profile router request failed: %s", exc)
        return LlmRouteResult(
            provider=profile.provider,
            model=profile.model,
            failure_type=_failure_type_for_exception(exc),
        )


def _parse_llm_route_result(raw: str) -> LlmRouteResult:
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return LlmRouteResult(failure_type="invalid_json")
    if not isinstance(data, dict):
        return LlmRouteResult(failure_type="invalid_response")
    profile_name = str(data.get("profile") or "").strip().lower()
    if profile_name not in LOCAL_ROUTE_PROFILES:
        return LlmRouteResult(failure_type="invalid_profile")
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return LlmRouteResult(failure_type="invalid_confidence")
    if not math.isfinite(confidence):
        return LlmRouteResult(failure_type="invalid_confidence")
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or "").strip()[:300]
    return LlmRouteResult(profile_name=profile_name, confidence=confidence, reason=reason)


def _failure_type_for_exception(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    return "timeout" if "timeout" in name else "request_error"


def _truncate_route_context(value: str, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[-limit:]


def _local_route_scores(user_prompt: str) -> dict[str, float]:
    query = _text_vector(user_prompt)
    if not query:
        return {}
    bm25_scores = _bm25_route_scores(query)
    cosine_scores = {
        profile: _cosine_similarity(query, vector)
        for profile, vector in _prototype_vectors().items()
    }
    # BM25 provides the primary lexical match. Cosine is deliberately kept as
    # a small, dependency-free tie-breaker for short or mixed-language turns.
    scores = {
        profile: (0.8 * bm25_scores.get(profile, 0.0)) + (0.2 * cosine_scores.get(profile, 0.0))
        for profile in LOCAL_ROUTE_PROFILES
    }
    # Character n-grams can lose meaning when a short phrase is split across
    # generic words. Keep a few high-signal phrase anchors as a tiny tie-breaker
    # rather than turning the router into a second intent grammar.
    normalized = " ".join(str(user_prompt or "").lower().split())
    for profile, anchors in _LOCAL_ROUTE_ANCHORS.items():
        anchor_bonus = sum(weight for phrase, weight in anchors if phrase in normalized) * 0.25
        if anchor_bonus:
            scores[profile] = min(1.0, scores[profile] + anchor_bonus)
    return scores


def _bm25_route_scores(query: Counter[str]) -> dict[str, float]:
    """Score the five static profile prototypes with an in-process BM25 pass.

    The corpus is tiny and immutable, so building an Elasticsearch index or a
    package-backed retriever would add startup latency without improving route
    quality. Scores are bounded to keep confidence and margin thresholds
    comparable across turns.
    """
    documents, document_frequency, average_length = _prototype_bm25_stats()
    if not documents or average_length <= 0:
        return {profile: 0.0 for profile in LOCAL_ROUTE_PROFILES}

    document_count = len(documents)
    raw_scores: dict[str, float] = {}
    for profile, document in documents.items():
        document_length = sum(document.values())
        score = 0.0
        for term in query:
            term_frequency = document.get(term, 0.0)
            if term_frequency <= 0:
                continue
            frequency = document_frequency.get(term, 0)
            inverse_document_frequency = math.log(
                1.0 + (document_count - frequency + 0.5) / (frequency + 0.5)
            )
            denominator = term_frequency + LOCAL_ROUTE_BM25_K1 * (
                1.0 - LOCAL_ROUTE_BM25_B
                + LOCAL_ROUTE_BM25_B * document_length / average_length
            )
            score += inverse_document_frequency * (
                term_frequency * (LOCAL_ROUTE_BM25_K1 + 1.0) / denominator
            )
        raw_scores[profile] = score

    if max(raw_scores.values(), default=0.0) <= 0:
        return {profile: 0.0 for profile in LOCAL_ROUTE_PROFILES}
    # Do not normalise by the best candidate in this query: that makes every
    # non-empty query look maximally confident. A fixed saturation keeps the
    # score comparable across turns and leaves the margin meaningful.
    return {
        profile: score / (score + 1.0) if score > 0 else 0.0
        for profile, score in raw_scores.items()
    }


def _explicit_route_profile(user_prompt: str) -> str | None:
    """Return only task contracts precise enough to bypass the model router."""
    value = " ".join(str(user_prompt or "").lower().split())
    if not value:
        return None

    no_implementation = _matches_any(value, (
        r"不要(?:实现|执行|修改|改动|改代码|写文件|修复)",
        r"不(?:实现|执行|修改|改动|改代码|写文件|修复)",
        r"do not (?:implement|execute|modify|edit|change|fix)",
        r"don't (?:implement|execute|modify|edit|change|fix)",
        r"wait for (?:my )?(?:approval|confirmation)",
        r"等我确认(?:后|再)",
        r"只(?:告诉|给)我.{0,6}(?:结论|问题|风险|清单)",
        r"findings only",
    ))
    plan_request = _matches_any(value, (
        r"先(?:给我|做|写|出)?.{0,8}(?:方案|计划)",
        r"只(?:给|要|输出).{0,6}(?:方案|计划|步骤)",
        r"(?:规划|制定|输出|给出|设计).{0,12}(?:方案|计划|路线|步骤)",
        r"(?:怎么|如何).{0,12}(?:实现|改造|迁移|设计|落地)",
        r"(?:拆成|分成).{0,8}(?:阶段|步骤)|技术路线|实施路径|落地路径|架构方案",
        r"\b(?:plan|proposal|roadmap|implementation approach|migration path)\b",
    ))
    review_request = _matches_any(value, (
        r"(?:只|仅)?(?:审查|审阅|评审).{0,16}(?:代码|实现|提交|变更|补丁|分支|pr|diff)?",
        r"检查.{0,10}(?:代码|提交|变更|补丁|分支|pr|diff).{0,12}(?:问题|风险|回归|安全)",
        r"\b(?:review|audit|inspect)\b.{0,16}(?:代码|实现|提交|变更|补丁|分支)",
        r"\b(?:review|audit|inspect)\b.{0,24}\b(?:code|implementation|pr|diff|patch|commit|branch)\b",
        r"\b(?:find issues|what risks?)\b.{0,24}\b(?:code|change|pr|diff|patch|commit)\b",
    ))
    explanation = _matches_any(value, (
        r"解释|是什么意思|说明一下|帮我理解|只告诉我原因|你是谁|你能做什么|为什么会|原理是什么",
        r"(?:总结|概括|比较|对比).{0,16}(?:内容|区别|优缺点|方案|结果|信息)?",
        r"^(?:你好|您好|嗨|hello|hi)\b",
        r"\b(?:explain|what does|what is|why does|help me understand|who are you|what can you do|summarize|compare)\b",
    ))
    mixed_delivery = _matches_any(value, (
        r"(?:并|然后|之后|解释后|审查后|评估后|发现问题).{0,12}(?:修复|修改|实现|优化|落地)",
        r"\b(?:and|then)\s+(?:fix|implement|modify|update|refactor)\b",
        r"^(?:implement|execute|apply).{0,16}\b(?:approved )?(?:plan|proposal)\b",
    ))
    mutation = _matches_any(
        value,
        (
            r"修复|修掉|修完|一并修|直接修|修(?:一下|一个|个)|改代码|修改|优化|调整|迁移|加功能|补上|落地|写代码",
            r"(?:帮我|请|直接|开始|继续|需要)(?:实现|完成)|实现(?:一个|该|这个|功能|模块|方案)",
            r"(?:帮我|请|直接|需要)(?:新增|增加|删除|移除|接入|完成|解决|处理|排查)",
            r"让.{0,16}(?:测试|构建|编译).{0,6}通过",
            r"\b(?:fix|implement|modify|refactor|update|optimize|migrate)\b",
            r"\b(?:please|can you|go ahead and)\s+(?:add|remove|delete|integrate|resolve)\b",
            r"\b(?:write|change) (?:the )?code\b|\bapply (?:a )?patch\b",
        ),
    )
    create = _matches_any(value, (
        r"(?:写|做|创建|搭建|开发|生成)(?:一个|个)",
        r"\b(?:build|create)(?:\s+(?:a|an))?\s+",
    ))
    web_product = _matches_any(value, (
        r"网页|网站|响应式|看板|小游戏|可视化|工作台|数据大屏|交互界面|前端应用",
        r"\b(?:web app|website|landing page|dashboard|browser app)\b",
    ))
    terminal_ui = _matches_any(value, (r"\btui\b", r"终端界面|命令行界面|terminal ui|\bcli\b"))

    if plan_request and no_implementation:
        return "plan"
    if review_request and no_implementation:
        return "review"
    if review_request and mutation:
        return "coding-agent"
    if explanation and no_implementation:
        return "general"
    if explanation and not mixed_delivery:
        return "general"
    if plan_request and not mixed_delivery:
        return "plan"
    if review_request and not mixed_delivery:
        return "review"
    if mutation and not no_implementation:
        return "coding-agent"
    if create and web_product and not terminal_ui:
        return "app-builder"
    if create and not web_product:
        return "coding-agent"
    return None


def _explicit_mode_profile(user_prompt: str) -> str | None:
    """Recognise a direct request to pin a workflow mode.

    Task wording such as ``审查这个改动`` remains an auto-routing contract;
    only explicit mode-selection language changes the routing mode in the
    caller. This keeps normal user requests from accidentally pinning a mode.
    """
    value = " ".join(str(user_prompt or "").lower().split())
    if not value:
        return None
    patterns = {
        "general": (r"(?:切换|回到|固定|使用).{0,8}(?:通用|普通|general)",),
        "coding-agent": (r"(?:切换|固定|使用).{0,8}(?:编码|coding|开发)模式",),
        "plan": (r"(?:切换|固定|使用).{0,8}(?:规划|计划|plan)模式",),
        "review": (r"(?:切换|固定|使用).{0,8}(?:审查|审阅|review)模式",),
        "app-builder": (r"(?:切换|固定|使用).{0,8}(?:应用构建|app|web)模式",),
    }
    for profile, profile_patterns in patterns.items():
        if _matches_any(value, profile_patterns):
            return profile
    return None


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


@lru_cache(maxsize=1)
def _prototype_vectors() -> dict[str, Counter[str]]:
    return {
        profile: _text_vector(text)
        for profile, text in _LOCAL_ROUTE_PROTOTYPES.items()
    }


@lru_cache(maxsize=1)
def _prototype_bm25_stats() -> tuple[dict[str, Counter[str]], Counter[str], float]:
    documents = _prototype_vectors()
    document_frequency: Counter[str] = Counter()
    lengths: list[float] = []
    for vector in documents.values():
        document_frequency.update(vector.keys())
        lengths.append(sum(vector.values()))
    average_length = sum(lengths) / len(lengths) if lengths else 0.0
    return documents, document_frequency, average_length


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
            for index in range(len(sequence) - ngram_size + 1):
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
        先聊聊 看看这个想法 回答即可 比较 对比 优缺点 含义 合理 错误信息 比较方案
        difference tradeoffs reasonable meaning message
    """,
    "coding-agent": """
        implement fix change update modify refactor migrate add feature edit files write code
        run tests verify failing test bug regression make the code pass patch repository
        修复 修改 实现 改代码 调整 迁移 加功能 跑测试 验证 落地 修 bug 提交补丁
        报错 失败 回归 恢复通过 解决问题 排除问题 行为不一致 动手处理 模块接入
        repository checks red failing test regression behavior expected result implementation hands-on
    """,
    "review": """
        review code review inspect changes find issues read-only assessment critique audit pull request diff
        findings severity risk regression missing tests security correctness maintainability
        审查 评审 review 看代码有没有问题 检查变更 找问题 只读评估 风险 严重程度
        风险评估 潜在回归 安全风险 边界情况 可维护性 正确性 补丁破坏现有行为 变更影响兼容性
        risk patch edge cases break existing behavior maintainability correctness
    """,
    "plan": """
        plan design proposal implementation plan architecture plan do not implement investigate and produce plan
        steps roadmap approach tradeoffs assumptions test plan decision complete
        计划 设计方案 实施方案 实现方案 不要实现 不要改代码 先规划 怎么做 路线 取舍 风险 假设
        实现步骤 阶段 技术路线 实施路径 落地路径 边界 依赖 验收方式 阶段拆分
        phase path assumptions validation dependencies acceptance criteria stages
    """,
    "app-builder": """
        build app create website web app page UI frontend component browser responsive design todo
        html css javascript react vite interface polished app game dashboard landing page
        做一个网页 创建应用 前端 UI 页面 浏览器 响应式 组件 看板 小游戏 可运行界面 todo
        侧边栏 实时筛选 数据展示 可视化工作台 交互界面 信息层次 日常使用
        polished workspace visual hierarchy interact preview visual workspace product surface
    """,
}


_LOCAL_ROUTE_ANCHORS = {
    "general": (
        ("difference between", 0.18),
        ("有什么区别", 0.18),
        ("help me understand", 0.18),
        ("是什么意思", 0.18),
        ("talk through", 0.14),
        ("先聊聊", 0.14),
    ),
    "coding-agent": (
        ("repository checks", 0.16),
        ("failing test", 0.16),
        ("hands-on work", 0.16),
        ("behavior no longer matches", 0.16),
        ("测试现在报错", 0.16),
        ("行为和预期不一致", 0.16),
    ),
    "review": (
        ("what risks", 0.18),
        ("edge cases", 0.18),
        ("break existing behavior", 0.18),
        ("maintainability and correctness", 0.18),
        ("潜在回归", 0.18),
        ("安全风险", 0.18),
        ("边界情况", 0.18),
        ("风险评估", 0.18),
    ),
    "plan": (
        ("each phase", 0.18),
        ("break this goal into stages", 0.18),
        ("acceptance criteria", 0.18),
        ("lay out the path", 0.16),
        ("经过哪些阶段", 0.18),
        ("技术路线", 0.18),
        ("验收方式", 0.18),
    ),
    "app-builder": (
        ("interact with and preview", 0.18),
        ("visual hierarchy", 0.18),
        ("product surface", 0.18),
        ("侧边栏和实时筛选", 0.18),
        ("数据展示界面", 0.18),
        ("可视化工作台", 0.18),
        ("浏览器里", 0.16),
    ),
}
