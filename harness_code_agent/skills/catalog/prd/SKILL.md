---
name: prd
description: Product requirements. Use when a new or fuzzy product/feature needs scope, non-goals, user workflows, acceptance criteria, constraints, or a first implementation slice defined before coding.
---

# Product Requirements

Own only `PRD.md`. Runtime planning owns execution state, retries, findings, and progress.

## 1. Establish product intent

Determine:

- target user and problem;
- primary scenario and expected outcome;
- constraints that change scope, risk, or user-visible behavior.

Inspect the repository before asking discoverable questions. Ask only unresolved questions that materially change the product.

Done when the goal can be stated in one testable sentence.

## 2. Draw the boundary

Separate:

- MVP behavior;
- explicit non-goals;
- deferred decisions;
- security, privacy, cost, deployment, or compatibility constraints.

Prefer the smallest independently valuable product slice. Do not reserve extension points for hypothetical needs.

Done when every requested capability is in scope, out of scope, or explicitly deferred.

## 3. Define observable acceptance

Write acceptance criteria as user-visible behavior with success and important failure outcomes. For UI work, include the minimum page map and primary flow needed to prevent implementation guesses.

Done when an implementer can verify every MVP behavior without interpreting intent.

## 4. Write `PRD.md`

Use Chinese by default unless the user requests another language. Preserve literal product names, APIs, commands, paths, and code symbols.

Use only the sections that carry decisions:

```md
# 项目 / 功能名称

## 目标与背景
## 目标用户与核心场景
## MVP 范围
## 验收标准
## 非目标
## 约束与风险
## 第一阶段实现切片
## 建议验证方式
```

Update an existing PRD rather than creating competing requirement documents. Keep implementation logs out.

## 5. Hand off to execution

Choose the lightest runtime planning mode for the first slice:

- `skip`: fewer than three expected tool calls and low risk;
- `light`: a short multi-step change;
- `full`: broad, uncertain, approval-sensitive, or high-risk work.

Pass only the current slice, relevant acceptance criteria, risks, and verification target into execution planning.

PRD work is complete when scope and non-goals are explicit, acceptance is observable, the first slice is independently verifiable, and `PRD.md` reflects the decisions.
