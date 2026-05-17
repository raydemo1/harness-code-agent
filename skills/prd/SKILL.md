---
name: prd
description: "Use this skill before Vibe Coding implementation when the user needs to define or refine product requirements, MVP scope, non-goals, acceptance criteria, user workflows, constraints, or the first implementation slice. This skill owns PRD.md only. Do not create ARCH.md or PROJECT_STATE.md; use planning-with-files for execution plans, findings, and progress logs."
---

# PRD

Use this skill when a project or feature needs a clear product requirements document before code is written.

The job is simple: turn a fuzzy idea into `PRD.md` with clear scope, boundaries, acceptance criteria, and the first verifiable implementation slice.

## Core Principle

`PRD.md` is the source of truth for what should be built and how success will be judged.

Keep execution memory out of the PRD. If the task needs phase tracking, research notes, retry logs, or session progress, use `planning-with-files` alongside this skill.

Do not create `ARCH.md` or `PROJECT_STATE.md` from this skill. Put only lightweight technical direction in `PRD.md`; create separate architecture docs only if the user explicitly asks.

## Collaboration With planning-with-files

Use `prd` and `planning-with-files` as a pair when a task moves from fuzzy product intent into multi-step execution.

`prd` owns:
- Product goal and background.
- Target users and core scenarios.
- MVP scope and non-goals.
- Acceptance criteria.
- User workflow and constraints.
- First implementation slice and suggested verification.

`planning-with-files` owns:
- `task_plan.md` phase breakdown and current status.
- `findings.md` research and codebase discoveries.
- `progress.md` session log, tool-level progress, errors, and verification results.
- Retry history and next actions.

After writing or updating `PRD.md`, hand off to `planning-with-files` when execution is complex, multi-step, research-heavy, or likely to take more than a few tool calls. The handoff should include:

```md
PRD goal:
Current slice:
Acceptance criteria:
Execution phases:
Suggested verification:
Known risks:
```

If implementation reveals a product decision changed, return to `PRD.md` and update the requirement source of truth before changing the execution plan. If only status, discovered facts, errors, or verification results changed, update the planning files only.

After the PRD pass, choose the planning mode for the first execution slice:
- `skip`: fewer than 3 estimated tool calls; no planning files required.
- `light`: 3-5 estimated tool calls; maintain `progress.md` only.
- `full`: more than 5 estimated tool calls; maintain `task_plan.md`, `findings.md`, and `progress.md`.

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
- Goal.
- Assumptions.
- In scope.
- Out of scope.
- Acceptance criteria.
- First slice.
- Open questions, only if blocking.

If execution will continue immediately, choose `skip`, `light`, or `full` with `planning-with-files` after the brief is written.

### Full PRD

Use for new projects, major features, or fuzzy MVPs.

Output:
- `PRD.md` only.
- First execution slice.
- Suggested verification.

If planning or progress tracking is needed, create or update the planning-with-files documents separately. Do not duplicate those logs in `PRD.md`.

### High-Risk PRD

Use for public products, multi-user systems, auth, payment, privacy, sensitive data, destructive data handling, production rollout, or expensive integrations.

Also include in `PRD.md`:
- Security and data boundaries.
- Non-functional requirements.
- Rollout and rollback expectations.
- Explicit deferrals.

## Workflow

### 1. Capture Intent

Identify:
- Target user.
- Problem or job to be done.
- Primary scenario.
- Core value.
- Desired MVP outcome.

Ask only questions that change scope, risk, architecture, or user-visible behavior.

### 2. Draw Boundaries

Separate:
- Must-have for the first usable version.
- Later enhancements.
- Non-goals.
- Open decisions.

Keep the MVP small enough to implement as one or more verifiable slices.

### 3. Define Acceptance Criteria

Write criteria as observable behavior.

Weak:
- "Supports login."

Strong:
- "A user can enter a valid email code, become authenticated, and land on the dashboard. Invalid or expired codes show a clear error. Rate limiting is either implemented or explicitly deferred."

### 4. Shape UX And Workflow

For UI products, define:
- Page or screen list.
- Main sections.
- Primary user flow.
- Navigation shape.
- Visual direction only as much as needed to avoid guessing.

### 5. Capture Constraints

Record constraints that affect implementation:
- Security, auth, privacy, payment, secrets.
- Data volume and performance.
- Local-only, internal, or public deployment.
- Cost limits and third-party services.
- Reliability and recovery expectations.

### 6. Choose A Practical Tech Direction

Prefer boring, inspectable, testable choices.

Consider:
- Existing repository conventions.
- Official docs and examples.
- Testing support.
- Deployment fit.
- Project size.

Avoid architecture for speculative future features. Keep this section lightweight unless the user asks for architecture work.

### 7. Write Or Update PRD.md

For substantial work, create or update `PRD.md`.

Keep the PRD concise. It is product context, not an execution diary.

Update it when scope, requirements, acceptance criteria, risks, or major product decisions change.

### 8. End With An Execution Contract

Every PRD pass should end with:
- Goal.
- In scope.
- Out of scope.
- Acceptance criteria.
- First implementation slice.
- Suggested verification.
- Known risks.
- Whether `planning-with-files` should be used next.
- Suggested planning mode for the first execution slice: `skip`, `light`, or `full`.

Default execution contract:
- Work one verifiable slice at a time.
- Do not expand scope without updating `PRD.md`.
- Preserve existing project conventions.
- Use planning-with-files for task plans, findings, progress, and retry logs when the task is long or complex.
- Verify with concrete commands or manual checks before claiming done.
- Report any verification that could not be run.

When handing off to `planning-with-files`, do not restate the entire PRD in planning files. Copy only the goal, current slice, acceptance criteria, verification target, and known risks needed to execute the slice.

## PRD.md Template

```md
# Project / Feature Name

## Goal And Background

## Target Users

## Core Scenarios

## MVP Scope

## Feature List

## Page / Workflow Map

## Acceptance Criteria

## Non-Goals

## Technical Direction

## Risks And Dependencies

## First Implementation Slice
```

## Done Criteria

PRD work is done when:
- The goal is clear.
- MVP and non-goals are separated.
- Acceptance criteria are testable.
- Important constraints are named or explicitly deferred.
- `PRD.md` is written or updated when useful.
- The first execution slice is ready.
- For complex execution, the handoff to `planning-with-files` is explicit.

## Examples

**New internal tool**
Input: "I want to build an internal knowledge-base search app. Help me plan it first."
Behavior: Define users, sources, access boundary, MVP, PRD, and first slice.

**Fuzzy MVP**
Input: "Let's make a tiny SaaS for invoice reminders, but I'm not sure what the MVP should be."
Behavior: Separate first-version workflows from later billing, templates, analytics, and admin features.

**Planning plus implementation**
Input: "Plan a local habit tracker and then start building it."
Behavior: Create a concise PRD, define the first slice, then proceed to implementation.
