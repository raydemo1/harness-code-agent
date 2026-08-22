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
    current = current_profile
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

    explicit_profile = _explicit_route_profile(user_prompt)
    if explicit_profile is not None:
        best_profile = explicit_profile
        best_score = 1.0
        margin = 1.0
        route_reason = f"Explicit instruction contract matched {best_profile}."
    else:
        scores = _local_route_scores(user_prompt)
        if not scores:
            return _with_elapsed(started_at, _local_fallback(current, "empty local route query"))

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        best_profile, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = max(0.0, best_score - second_score)
        route_reason = f"Local semantic prototype matched {best_profile}."

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
                reason=route_reason,
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
            reason=route_reason,
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
    scores = {
        profile: _cosine_similarity(query, vector)
        for profile, vector in _prototype_vectors().items()
    }
    # Character n-grams are deliberately dependency-free, but they can lose
    # meaning when a short phrase is split across generic words.  Add a small
    # set of profile-specific phrase anchors to preserve those high-signal
    # local cues without turning the router into a second intent grammar.
    normalized = " ".join(str(user_prompt or "").lower().split())
    for profile, anchors in _LOCAL_ROUTE_ANCHORS.items():
        anchor_bonus = sum(weight for phrase, weight in anchors if phrase in normalized)
        if anchor_bonus:
            scores[profile] = min(1.0, scores[profile] + anchor_bonus)
    return scores


def _explicit_route_profile(user_prompt: str) -> str | None:
    """Resolve explicit user contracts before fuzzy semantic similarity.

    Mutation authority, read-only constraints, and workflow intent are separate
    signals. A requested mutation wins over review wording; an explicit
    plan-only or no-edit constraint wins over words such as implement or fix.
    """
    value = " ".join(str(user_prompt or "").lower().split())
    if not value:
        return None

    plan_only = _matches_any(
        value,
        (
            r"不要(?:实现|执行)",
            r"先(?:给我|做|写|出)?.{0,8}(?:方案|计划)",
            r"(?:先|动手前|开始前).{0,12}(?:梳理|明确|制定|规划|拆解|输出|给出).{0,18}(?:方案|计划|步骤|阶段|路线|边界|依赖|验收|取舍|风险)",
            r"(?:目标|任务).{0,10}(?:需要|应该|可以).{0,10}(?:经过|拆成|分成).{0,10}(?:阶段|步骤)",
            r"(?:技术|实施|落地)路线(?:和|与)?(?:风险|依赖|验收)?",
            r"等我确认(?:后|再)",
            r"do not (?:implement|execute)",
            r"(?:plan|design) only",
            r"wait for (?:my )?(?:approval|confirmation)",
        ),
    )
    read_only = _matches_any(
        value,
        (
            r"只(?:审查|审阅|检查|评估|分析)",
            r"不要(?:修改|改动|改代码|写文件|修复)",
            r"不(?:修改|改动|改代码|写文件|修复)",
            r"read[ -]?only",
            r"(?:review|audit|inspect) only",
            r"do not (?:modify|edit|change)",
            r"don't (?:modify|edit|change)",
        ),
    )
    mutation = _matches_any(
        value,
        (
            r"修复|修掉|修完|一并修|直接修|改代码|修改|优化|调整|迁移|加功能|补上|落地|写代码",
            r"(?:并|然后|直接|帮我|请)实现|实现(?:一个|该|这个|功能|模块)",
            r"(?:测试|报错|失败|回归).{0,16}(?:恢复|通过|修复|解决|处理|排除)",
            r"(?:测试|报错|失败|回归|bug).{0,20}(?:原因|定位|下手|怎么查|如何处理)",
            r"行为(?:和|与).{0,8}(?:不一致|不符)|需要动手(?:处理|修复|改)",
            r"\b(?:fix|implement|modify|refactor|update|optimize|migrate)\b",
            r"\b(?:write|change) (?:the )?code\b",
            r"\b(?:apply|submit|prepare|write) (?:a )?patch\b|\bpatch (?:the|this|that) (?:code|file|bug)\b",
        ),
    )
    create = _matches_any(value, (r"做一个|创建(?:一个|应用|项目|页面)|新增(?:一个|功能)", r"\b(?:build|create)\b"))
    web_app = _matches_any(
        value,
        (
            r"网页|网站|前端|页面|浏览器|响应式|组件|看板|小游戏|界面|可视化|工作台|侧边栏|筛选|数据展示",
            r"(?:交互|操作|预览).{0,12}(?:界面|结果|浏览器)",
            r"\b(?:interact|preview|visual workspace|product surface)\b",
            r"\b(?:web app|website|react|frontend|landing page|dashboard)\b",
        ),
    )
    terminal_ui = _matches_any(value, (r"\btui\b", r"终端界面|命令行界面|terminal ui"))
    review = _matches_any(
        value,
        (
            r"审查|审阅|评审|代码有没有|安全问题|检查.{0,10}(?:问题|风险)",
            r"风险评估|潜在回归|安全风险|边界情况|可维护性|正确性",
            r"(?:提交|补丁|变更|代码).{0,20}(?:风险|问题|回归|安全|正确性|可维护性|破坏)",
            r"(?:破坏|影响).{0,10}(?:现有行为|兼容性)",
            r"\b(?:what risks?|edge cases?|break existing behavior|maintainability|correctness|patch)\b",
            r"\b(?:review|audit|critique|inspect)\b",
        ),
    )
    explain = _matches_any(value, (r"解释|是什么意思|说明一下|帮我理解", r"\b(?:explain|what does|help me understand)\b"))
    plan = _matches_any(
        value,
        (
            r"(?:先|需要|可以|应该|请|帮我|把|从).{0,12}(?:梳理|明确|制定|规划|拆解|设计|输出|给出).{0,16}(?:方案|计划|步骤|阶段|路线|边界|依赖|验收|取舍|风险)",
            r"(?:需要|可以|应该).{0,12}(?:经过|拆成|分成).{0,12}(?:阶段|步骤)",
            r"(?:方案|计划|步骤|阶段|路线|验收方式).{0,15}(?:如何|怎么|先|之前|取舍|依赖|风险|验证|落地)",
            r"技术路线|实施路径|落地路径|阶段拆分",
            r"\b(?:how|what).{0,20}(?:break|split).{0,12}(?:goal|task).{0,12}(?:stages|steps)\b",
            r"\b(?:each|every) phase\b|\bbefore we start\b",
            r"\b(?:plan|design|proposal|roadmap)\b",
        ),
    )

    if plan_only:
        return "plan"
    if not read_only and (mutation or create):
        if web_app and not terminal_ui:
            return "app-builder"
        return "coding-agent"
    if explain:
        return "general"
    if review:
        return "review"
    if plan:
        return "plan"
    if web_app and not terminal_ui:
        return "app-builder"
    return None


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


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
