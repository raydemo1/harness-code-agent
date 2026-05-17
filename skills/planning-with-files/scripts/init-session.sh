#!/bin/bash
# Initialize planning files for a new session.
# Usage: ./init-session.sh [project-name]

set -e

PROJECT_NAME="${1:-project}"
DATE=$(date +%Y-%m-%d)

echo "Initializing planning files for: $PROJECT_NAME"

if [ ! -f "task_plan.md" ]; then
    cat > task_plan.md << 'EOF'
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
EOF
    echo "Created task_plan.md"
else
    echo "task_plan.md already exists, skipping"
fi

if [ ! -f "findings.md" ]; then
    cat > findings.md << 'EOF'
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
EOF
    echo "Created findings.md"
else
    echo "findings.md already exists, skipping"
fi

if [ ! -f "progress.md" ]; then
    cat > progress.md << EOF
# Progress Log

## Session: $DATE

## Current State

- Goal:
- Current phase:
- Current step:
- Next action:
- Blockers:

This is the single progress file for light and full planning modes. Update it through \`update_planning_files\`.

## PRD / Plan Alignment

- PRD source: \`PRD.md\` / none / not needed
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
| Where am I? | Current phase in \`task_plan.md\` |
| Where am I going? | Remaining phases in \`task_plan.md\` |
| What's the goal? | Goal in \`task_plan.md\` and, if present, \`PRD.md\` |
| What have I learned? | \`findings.md\` |
| What have I done? | This file |

Update this file after completing each phase, encountering errors, and running verification.
EOF
    echo "Created progress.md"
else
    echo "progress.md already exists, skipping"
fi

echo ""
echo "Planning files initialized."
echo "Files: task_plan.md, findings.md, progress.md"
