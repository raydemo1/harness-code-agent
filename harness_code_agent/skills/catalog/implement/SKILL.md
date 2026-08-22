---
name: implement
description: Implement one approved PRD slice or agent-ready issue.
argument-hint: "<PRD path, issue, or requested slice>"
disable-model-invocation: true
---

# Implement

Implement exactly one approved slice.

1. Read the referenced PRD or issue and the repository instructions.
2. State the observable target, exclusions, and verification command. Resolve only decisions that block implementation.
3. Load `tdd` when the change needs test-first development and `vibe-execution-guard` when scope or risk requires a hard boundary.
4. Implement the smallest vertical slice through the real public interface.
5. Run focused checks during the change, then the strongest relevant final verification.
6. Report changed behavior, verification evidence, and remaining issues. Commit only when the user requested a commit.

Done when the requested slice is observable through its public interface, its acceptance checks pass, and unrelated scope is untouched.
