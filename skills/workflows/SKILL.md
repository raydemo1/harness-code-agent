---
name: workflows
description: Choose the right user-invoked engineering workflow.
argument-hint: "<situation>"
disable-model-invocation: true
---

# Workflows

Use this router when you know the situation but not which `/name` workflow fits.

## Build or change something

1. Run `/setup-workflows` once if issue-tracker and domain-doc conventions are not configured.
2. Use `/grill-with-docs` when an existing codebase needs terminology or architectural decisions sharpened. Use `/grill-me` when no repository context is needed.
3. Create or update `PRD.md` when product scope is still fuzzy; the model-invoked `prd` skill owns that artifact.
4. Use `/to-issues` for work that should be split into independent tracer-bullet issues.
5. Use `/implement` for one approved PRD slice or one agent-ready issue.

## Incoming work

- Use `/triage` for raw issues or external pull requests.
- Do not triage issues produced by `/to-issues`; they are already agent-ready.

## Codebase health

- Use `/improve-codebase-architecture` to find deepening opportunities and choose one for a later implementation flow.

## Context boundaries

- Use `/handoff` when a fresh session needs the current context.
- Use built-in `/compact` only at a phase boundary when continuing in the same session.

## Skill maintenance

- Use `/writing-great-skills` when creating or pruning skills.

Done when the user has one recommended next command and understands why it fits.
