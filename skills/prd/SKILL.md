---
name: prd
description: "Use this skill before Vibe Coding implementation when the user needs to define or refine product requirements, MVP scope, non-goals, acceptance criteria, user workflows, constraints, or the first implementation slice. This skill owns PRD.md only. PRD.md is user-facing and must be written in Chinese by default. Do not create ARCH.md, PROJECT_STATE.md, execution plans, findings logs, or progress logs from this skill."
---

# PRD

Use this skill when a project or feature needs a clear product requirements document before code is written.

The job is simple: turn a fuzzy idea into `PRD.md` with clear scope, boundaries, acceptance criteria, and the first verifiable implementation slice.

## Language Policy

`PRD.md` 是给用户看的产品文档。默认使用中文撰写，包括标题、要点、示例、假设、验收标准、风险和建议验证方式。

只有在产品名、API 名、命令、文件路径、代码符号、库、框架、协议或用户原话需要保留字面值时，才使用英文。

如果用户明确要求其他语言，按用户要求执行。否则不要在 `PRD.md` 里使用 "Goal"、"MVP Scope"、"Acceptance Criteria"、"First Implementation Slice" 等英文标题；使用下方中文模板。

## Core Principle

`PRD.md` is the source of truth for what should be built and how success will be judged.

Keep execution memory out of the PRD. If the task needs phase tracking, research notes, retry logs, or session progress, use the runtime Planning Mode Self-Check and `update_plan_state` after the PRD pass.

Do not create `ARCH.md` or `PROJECT_STATE.md` from this skill. Put only lightweight technical direction in `PRD.md`; create separate architecture docs only if the user explicitly asks.

## Collaboration With Runtime Planning

Use `prd` for product clarity, then let the runtime planning policy choose the lightest execution mode for the first implementation slice.

`prd` owns:
- 产品目标和背景。
- 目标用户和核心场景。
- MVP 范围和非目标。
- 验收标准。
- 用户工作流和约束。
- 第一阶段实现切片和建议验证方式。

Runtime planning owns:
- `skip`、`light`、`full` 的执行模式选择。
- `update_plan_state` 中的当前步骤、已完成步骤、阻塞项、下一步行动、重试原因和验证结果。
- 需要用户确认时的 `global_plan/current/plan.md`。

After writing or updating `PRD.md`, hand off to runtime planning when execution is complex, multi-step, research-heavy, or likely to take more than a few tool calls. The handoff context should include:

```md
PRD 目标：
当前切片：
验收标准：
执行阶段：
建议验证方式：
已知风险：
```

If implementation reveals a product decision changed, return to `PRD.md` and update the requirement source of truth before changing the execution plan. If only status, discovered facts, errors, or verification results changed, update runtime planning state only.

After the PRD pass, choose the planning mode for the first execution slice:
- `skip`: fewer than 3 estimated tool calls; no planning artifact required.
- `light`: 3-5 estimated tool calls; call `update_plan_state(update_kind="start")` before tracked actions.
- `full`: more than 5 estimated tool calls or higher-risk execution; call `update_plan_state(update_kind="start", requires_approval=true)` with `plan_markdown`, then wait for confirmation.

Do not force full planning just because `PRD.md` exists. `PRD.md` defines scope and acceptance; the execution slice still chooses the lightest planning mode that fits.

## When To Use

Use this skill when the user asks to:
- Start a new app, website, tool, workflow, or product.
- Clarify a fuzzy feature before coding.
- Define MVP scope, non-goals, or acceptance criteria.
- Create or update `PRD.md`.
- Choose a practical product direction, page map, user flow, or first implementation slice.
- Turn a broad request into implementation-ready requirements.
- Prepare reusable product context for future AI/harness runs.

## When Not To Use

Do not use this skill for:
- A specific bug fix with clear repro steps.
- An already-scoped implementation task with an existing PRD.
- Code review without requested edits.
- Simple command execution or status checks.
- Pure explanation or summarization.
- Documentation polishing that does not affect product scope or acceptance criteria.

If the user wants to skip planning, do the shortest useful PRD pass: record assumptions, define the first slice, and move on.

## Output Modes

### Quick PRD Brief

Use for small features or existing projects with limited uncertainty.

Output inside `PRD.md`:
- 目标。
- 假设。
- 范围内。
- 范围外。
- 验收标准。
- 第一阶段实现切片。
- 待确认问题，仅保留阻塞项。

If execution will continue immediately, choose `skip`, `light`, or `full` with the runtime Planning Mode Self-Check after the brief is written.

### Full PRD

Use for new projects, major features, or fuzzy MVPs.

Output:
- `PRD.md` only.
- 第一阶段执行切片。
- 建议验证方式。

If planning or progress tracking is needed, update runtime planning state separately. Do not duplicate execution logs in `PRD.md`.

### High-Risk PRD

Use for public products, multi-user systems, auth, payment, privacy, sensitive data, destructive data handling, production rollout, or expensive integrations.

Also include in `PRD.md`:
- 安全和数据边界。
- 非功能需求。
- 发布与回滚预期。
- 明确延后事项。

## Workflow

### 1. 捕捉意图

识别：
- 目标用户。
- 要解决的问题或用户任务。
- 主要使用场景。
- 核心价值。
- 期望的 MVP 结果。

Ask only questions that change scope, risk, architecture, or user-visible behavior.

### 2. 划定边界

区分：
- 第一版必须具备的能力。
- 后续增强能力。
- 非目标。
- 尚未确定的决策。

Keep the MVP small enough to implement as one or more verifiable slices.

### 3. 定义验收标准

Write criteria as observable behavior.

弱示例：
- “支持登录。”

强示例：
- “用户可以输入有效邮箱验证码完成认证并进入仪表盘。无效或过期验证码会显示清晰错误。限流要么已实现，要么明确延后。”

### 4. 梳理体验和工作流

如果是 UI 产品，定义：
- 页面或屏幕清单。
- 主要区块。
- 主要用户流程。
- 导航结构。
- 必要的视觉方向，只写到足以避免实现者猜测即可。

### 5. 记录约束

记录会影响实现的约束：
- 安全、认证、隐私、支付、密钥。
- 数据量和性能。
- 本地使用、内部使用或公开部署。
- 成本限制和第三方服务。
- 可靠性和恢复预期。

### 6. 选择务实的技术方向

Prefer boring, inspectable, testable choices.

考虑：
- 现有仓库约定。
- 官方文档和示例。
- 测试支持。
- 部署匹配度。
- 项目规模。

Avoid architecture for speculative future features. Keep this section lightweight unless the user asks for architecture work.

### 7. 编写或更新 PRD.md

For substantial work, create or update `PRD.md`.

Keep the PRD concise. It is product context, not an execution diary.

Update it when scope, requirements, acceptance criteria, risks, or major product decisions change.

### 8. 以执行约定收尾

Every PRD pass should end with:
- 目标。
- 范围内。
- 范围外。
- 验收标准。
- 第一阶段实现切片。
- 建议验证方式。
- 已知风险。
- 是否接下来进入执行，以及建议的 runtime planning mode。
- 第一阶段执行切片建议使用的 planning mode：`skip`、`light` 或 `full`。

Default execution contract:
- 一次只推进一个可验证切片。
- 未更新 `PRD.md` 前不要扩展范围。
- 保持现有项目约定。
- 任务较长或复杂时，用 `update_plan_state` 记录当前步骤、阻塞项、重试原因、验证结果和剩余问题。
- 声称完成前，用具体命令或人工检查验证。
- 如有无法运行的验证，必须说明。

交接给 runtime planning 时，不要复述整份 PRD。只保留执行当前切片所需的目标、当前切片、验收标准、验证目标和已知风险。

## PRD.md Template

```md
# 项目 / 功能名称

## 目标与背景

## 目标用户

## 核心场景

## MVP 范围

## 功能清单

## 页面 / 工作流地图

## 验收标准

## 非目标

## 技术方向

## 风险与依赖

## 第一阶段实现切片
```

## 完成标准

PRD 工作完成的标准：
- 目标清楚。
- MVP 和非目标已区分。
- 验收标准可测试。
- 重要约束已写明或明确延后。
- 需要时已经写入或更新 `PRD.md`。
- 第一阶段执行切片已经明确。
- 对复杂执行，已经明确建议 runtime planning mode。

## 示例

**新的内部工具**
输入："I want to build an internal knowledge-base search app. Help me plan it first."
行为：用中文定义用户、数据来源、访问边界、MVP、PRD 和第一阶段切片。

**模糊 MVP**
输入："Let's make a tiny SaaS for invoice reminders, but I'm not sure what the MVP should be."
行为：用中文区分第一版工作流和后续的计费、模板、分析、管理后台等能力。

**规划加实现**
输入："Plan a local habit tracker and then start building it."
行为：用中文创建简洁 PRD，定义第一阶段切片，然后进入实现。
