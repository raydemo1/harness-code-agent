---
name: vibe-execution-guard
description: "Use this skill only for high-risk or tightly bounded execution in Vibe Coding: user-specified 'only change this' constraints, auth/permissions/secrets/payment/privacy, destructive data changes, migrations, production incidents, rollback, or bugs that survived repeated fixes. It locks scope, identifies risk and rollback points, requires fact-based debugging, and verifies the smallest safe patch. Do not use for ordinary implementation when scope is already clear, project kickoff, PRD work, code review without edits, simple explanations, or status checks."
---

# Vibe Execution Guard

Use this skill only when execution needs a stricter boundary than normal implementation.

Most coding tasks do not need this skill. A good kickoff document plus normal verification is enough. Use this when the cost of accidental scope expansion, guessing, or unsafe edits is high.

## Core Principle

Lock scope, inspect facts, make the smallest safe change, verify before claiming success.

## When To Use

Use this skill when:
- The user says "only change this", "do not touch UI", "do not change unrelated logic", or similar.
- The task touches auth, authorization, secrets, payment, privacy, tenant boundaries, or user data.
- The task involves migrations, destructive writes, rollback, production incidents, or irreversible changes.
- A bug survived one or two attempted fixes and needs systematic debugging.
- The requested change is small but sits in a risky part of the system.

## When Not To Use

Do not use this skill for:
- Ordinary implementation with clear scope and low risk.
- New project planning or PRD/architecture work.
- Pure code review without edits.
- Simple command execution, summaries, or explanations.
- UI polish, copy edits, or documentation-only changes with no behavior risk.

## Procedure

### 1. Lock The Scope

State:
- Target behavior.
- Files or modules likely in scope.
- Explicitly excluded areas.
- Acceptance criteria.
- Smallest useful verification.

If the user already gave boundaries, preserve them. Ask only if the missing answer changes safety, data, or user-visible behavior.

### 2. Identify Risk And Rollback

Before editing, name:
- What can break.
- What data or users could be affected.
- The safest rollback point.
- Whether temporary diagnostics are acceptable.

### 3. Inspect Before Editing

Read nearby code, tests, and conventions. Search for existing constants, config, helpers, and similar behavior before introducing new ones.

Do not refactor unrelated code. Do not broaden the change just because nearby code looks messy.

### 4. Debug With Facts

For unstable bugs:
- Reproduce the failure.
- Compare input, intermediate state, and actual output.
- Add temporary logging only to answer a concrete question.
- Prefer a regression test when practical.
- Stop guessing after repeated failed fixes.

### 5. Patch The Smallest Cause

Change only what explains the observed problem or requested boundary.

If the fix requires a wider change than expected, pause and restate the new scope before continuing.

### 6. Verify

Run the smallest meaningful check:
- Focused test.
- Relevant integration or regression test.
- Typecheck or lint when useful.
- Manual repro if no automated check exists.

Remove temporary diagnostics unless the user asked to keep them.

## Response Shape

Use a compact structure:

```md
Target:
Locked scope:
Out of scope:
Risk / rollback:
Verification:
```

For very small boundary checks, keep it shorter.

## Done Criteria

The guarded execution is done when:
- The target behavior is implemented.
- Explicit boundaries were respected.
- No unrelated refactor was introduced.
- Verification ran, or the missing verification is reported.
- Remaining risk is stated plainly.

## Examples

**Strict boundary**
Input: "Only change the backend permission check. Do not touch UI."
Behavior: Restrict edits to the permission path unless tests or shared fixtures must change.

**Sensitive code**
Input: "The tenant isolation bug leaks another customer's records."
Behavior: Reproduce, inspect authorization/data boundaries, patch the smallest cause, and add a regression check.

**Repeated bug**
Input: "This form 500 has survived two fixes already."
Behavior: Stop guessing, create a minimal repro, inspect facts, then patch.

**Rollback**
Input: "Roll back the experimental caching change but keep any valid tests."
Behavior: Identify rollback point, remove the risky behavior, preserve useful checks, and verify.
