# Task Plan: PRD Phase 2 Observability

## Goal
Implement PRD phase 2 as usable session observability: classified failures plus a final report event that supports statistics, evaluation, and replay.

## Current Phase
Phase 5

## Phases

### Phase 1: Requirements & Discovery
- [x] Read PRD phase 2 scope.
- [x] Locate existing ToolResult, event schema, summary, CLI, and tool execution code.
- [x] Document findings in findings.md.
- **Status:** complete

### Phase 2: Red Tests
- [x] Add focused failing tests for failure classification.
- [x] Add focused failing tests for enhanced final report events and summary consumption.
- **Status:** complete

### Phase 3: Implementation
- [x] Implement reusable failure classification.
- [x] Add FinalReportEvent with statistics-ready payload.
- [x] Emit final report on interactive session close.
- [x] Teach summary to prefer final_report while keeping task_outcome compatibility.
- **Status:** complete

### Phase 4: Verification
- [x] Run focused product/runtime and interactive CLI tests.
- [x] Run the full test suite if focused tests pass.
- **Status:** complete

### Phase 5: Delivery
- [x] Review git diff and explain touched files.
- [x] Report verification evidence and any residual gaps.
- **Status:** complete

## Key Questions
1. Should existing task_outcome remain? Yes, preserve compatibility and add final_report as the phase 2 event.
2. Should classifications be complex? No, PRD asks simple categories: tool_error, runtime_error, user_cancelled, validation_error, unknown.

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Add final_report rather than replace task_outcome | Existing tests and summary already support task_outcome; phase 2 needs a richer event without breaking old data. |
| Classify from structured status_source/reason first, message text second | ToolResult metadata already carries native/validation/exception/approval/registry sources, which is more stable than parsing output. |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|

## Notes
- PRD.md is currently untracked user-provided context; do not edit or stage it unless asked.
