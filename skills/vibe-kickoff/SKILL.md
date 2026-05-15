---
name: vibe-kickoff
description: "Use this skill before Vibe Coding implementation when the user is starting a new project, shaping a fuzzy feature, defining MVP scope, writing PRD-style context, setting acceptance criteria, choosing architecture or tech stack, mapping pages/workflows, or creating durable project context for future agent runs. It turns ambiguity into concrete docs, boundaries, and the first verifiable execution slice. Do not use for already-scoped bug fixes, routine implementation, code review, simple explanations, or status checks unless requirements or project direction need to be clarified."
---

# Vibe Kickoff

Use this skill when a project or feature needs a stable foundation before code is written.

The job is simple: turn a fuzzy idea into durable context, clear boundaries, acceptance criteria, and a first execution slice.

## Core Principle

Requirements should land somewhere durable. If the agent only chats through the plan, the next run will have to rediscover it.

Plan only enough to make implementation safe and verifiable. Do not turn small edits into a ceremony.

## When To Use

Use this skill when the user asks to:
- Start a new app, website, tool, workflow, or product.
- Clarify a fuzzy feature before coding.
- Define MVP scope, non-goals, or acceptance criteria.
- Create or update project context documents.
- Choose a practical architecture, data model, page map, or tech stack.
- Turn a broad request into implementation slices.
- Prepare a reusable context for future AI/harness runs.

## When Not To Use

Do not use this skill for:
- A specific bug fix with clear repro steps.
- An already-scoped implementation task.
- Code review without requested edits.
- Simple command execution or status checks.
- Pure explanation or summarization.
- Documentation polishing that does not affect project direction.

If the user wants to skip planning, do the shortest useful kickoff: record assumptions, define the first slice, and move on.

## Output Modes

### Quick Brief

Use for small features or existing projects with limited uncertainty.

Output:
- Understanding.
- Assumptions.
- First slice.
- Acceptance criteria.
- Open questions, only if blocking.

### Project Foundation

Use for new projects or major features.

Output:
- `PRD.md` draft.
- `ARCH.md` draft.
- `PROJECT_STATE.md` draft.
- First execution slice.
- Quality checks.

Write files when the user asks for files or when the context is stable enough to preserve.

### High-Risk Foundation

Use for public products, multi-user systems, auth, payment, privacy, sensitive data, destructive data handling, production rollout, or expensive integrations.

Also include:
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

Avoid architecture for speculative future features.

### 7. Create Durable Context

For substantial work, create or update:
- `PRD.md`
- `ARCH.md`
- `PROJECT_STATE.md`

Keep these documents concise. They are working context, not a novel.

Update them when scope, architecture, current goal, known issues, or major decisions change.

### 8. End With An Execution Contract

Every kickoff should end with:
- Goal.
- In scope.
- Out of scope.
- Acceptance criteria.
- Suggested verification.
- Known risks.

Default execution contract:
- Work one verifiable slice at a time.
- Do not expand scope without updating the project state.
- Preserve existing project conventions.
- Verify with concrete commands or manual checks before claiming done.
- Report any verification that could not be run.

## Document Templates

### `PRD.md`

```md
# Project Name
## Goal And Background
## Target Users
## Core Scenarios
## MVP Scope
## Feature List
## Page / Workflow Map
## Acceptance Criteria
## Non-Goals
## Risks And Dependencies
```

### `ARCH.md`

```md
# Architecture
## Tech Stack
## System Boundaries
## Directory Structure
## Core Modules
## Data Model / Core Entities
## Frontend And Backend Responsibilities
## Security And Deployment Notes
## Expected Evolution Points
```

### `PROJECT_STATE.md`

```md
# Project State
## Current Phase
## Current Goal
## Completed
## Next Step
## Known Issues
## Decisions
## Open Questions
```

## Done Criteria

Kickoff is done when:
- The goal is clear.
- MVP and non-goals are separated.
- Acceptance criteria are testable.
- Important constraints are named or explicitly deferred.
- Durable context is written when useful.
- The first execution slice is ready.

## Examples

**New internal tool**
Input: "I want to build an internal knowledge-base search app. Help me plan it first."
Behavior: Define users, sources, access boundary, MVP, architecture, docs, and first slice.

**Fuzzy MVP**
Input: "Let's make a tiny SaaS for invoice reminders, but I'm not sure what the MVP should be."
Behavior: Separate first-version workflows from later billing, templates, analytics, and admin features.

**Planning plus implementation**
Input: "Plan a local habit tracker and then start building it."
Behavior: Create a short foundation, define the first slice, then proceed to implementation.
