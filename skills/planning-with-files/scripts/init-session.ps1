# Initialize planning files for a new session.
# Usage: .\init-session.ps1 [project-name]

param(
    [string]$ProjectName = "project"
)

[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)

$Date = Get-Date -Format "yyyy-MM-dd"

Write-Host "Initializing planning files for: $ProjectName"

if (-not (Test-Path "task_plan.md")) {
    @"
# Task Plan: [Brief Description]

## PRD Link

- PRD source: `PRD.md` / none / not needed
- PRD goal:
- Current implementation slice:
- Acceptance criteria to verify:

Use `PRD.md` as the source of truth for product scope. If scope or acceptance criteria change, update `PRD.md` with the `prd` skill, then update this plan.

## Goal

[One sentence describing the execution end state]

## Current Phase

Phase 1

## Phases

### Phase 1: Requirements And Discovery

- [ ] Read `PRD.md` if it exists
- [ ] Understand the user's latest request
- [ ] Identify constraints, risks, and likely files
- [ ] Document findings in `findings.md`
- **Status:** in_progress

### Phase 2: Execution Plan

- [ ] Define the smallest useful implementation path
- [ ] Record decisions and tradeoffs
- [ ] Call `update_planning_files` in full mode before substantive action tools
- **Status:** pending

### Phase 3: Implementation

- [ ] Make scoped changes
- [ ] Update findings as discoveries appear
- [ ] Track errors and changed approaches
- **Status:** pending

### Phase 4: Verification

- [ ] Run focused tests or manual checks
- [ ] Record results in `progress.md`
- [ ] Fix issues found during verification
- **Status:** pending

### Phase 5: Delivery

- [ ] Confirm phases reflect reality
- [ ] Confirm `progress.md` has final state
- [ ] Summarize changes and verification for the user
- **Status:** pending

## Key Questions

1. [Question to answer]
2. [Question to answer]

## Decisions Made

| Decision | Rationale |
|---|---|
| | |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---|---|
| | 1 | |

## Files Changed

| File | Reason |
|---|---|
| | |

## Notes

- Update phase status as work progresses: `pending` -> `in_progress` -> `complete`.
- Re-read this plan before major decisions.
- Log all meaningful errors so failed approaches are not repeated.
- Keep product scope in `PRD.md`; keep execution memory here.
"@ | Out-File -FilePath "task_plan.md" -Encoding utf8NoBOM
    Write-Host "Created task_plan.md"
} else {
    Write-Host "task_plan.md already exists, skipping"
}

if (-not (Test-Path "findings.md")) {
    @"
# Findings And Decisions

## Requirements Context

- PRD source: `PRD.md` / none / not needed
- Current slice:
- Acceptance criteria:

Keep product requirements in `PRD.md`. Use this file for discoveries that affect execution.

## Research Findings

-

## Codebase Findings

-

## Technical Decisions

| Decision | Rationale |
|---|---|
| | |

## Issues Encountered

| Issue | Resolution |
|---|---|
| | |

## Resources

-

## Visual Or Browser Findings

-

Update this file after every two view/browser/search operations, and whenever a discovery changes the implementation path.
"@ | Out-File -FilePath "findings.md" -Encoding utf8NoBOM
    Write-Host "Created findings.md"
} else {
    Write-Host "findings.md already exists, skipping"
}

if (-not (Test-Path "progress.md")) {
    @"
# Progress Log

## Session: $Date

## Current State

- Goal:
- Current phase:
- Current step:
- Next action:
- Blockers:

This is the single progress file for light and full planning modes. Update it through `update_planning_files`.

## PRD / Plan Alignment

- PRD source: `PRD.md` / none / not needed
- Current slice:
- Acceptance criteria checked:

## Actions Taken

-

## Files Created Or Modified

-

## Verification Results

| Check | Expected | Actual | Status |
|---|---|---|---|
| | | | |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|---|---|---|---|
| | | 1 | |

## 5-Question Reboot Check

| Question | Answer |
|---|---|
| Where am I? | Current phase in `task_plan.md` |
| Where am I going? | Remaining phases in `task_plan.md` |
| What's the goal? | Goal in `task_plan.md` and, if present, `PRD.md` |
| What have I learned? | `findings.md` |
| What have I done? | This file |

Update this file after completing each phase, encountering errors, and running verification.
"@ | Out-File -FilePath "progress.md" -Encoding utf8NoBOM
    Write-Host "Created progress.md"
} else {
    Write-Host "progress.md already exists, skipping"
}

Write-Host ""
Write-Host "Planning files initialized."
Write-Host "Files: task_plan.md, findings.md, progress.md"
