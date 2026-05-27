---
name: headless-terminal
description: Guide for implementing and testing headless terminal or pseudo-terminal interfaces. Use when building shell session wrappers, terminal emulators, persistent shell runners, expect/send automation, command streaming, Ctrl+C handling, prompt detection, or terminal output capture.
---

# Headless Terminal Implementation

Use this skill when implementing code that programmatically drives a shell or
terminal session without a visible UI. The goal is a reliable interface for
spawning a shell, sending input, reading output, handling lifecycle, and testing
interactive behavior.

## Workflow

1. Read the interface contract.
   Identify required methods, return values, timeout behavior, cancellation
   semantics, encoding, cleanup, and whether state must persist between commands.

2. Choose the backend deliberately.

| Backend | Use When | Tradeoff |
| --- | --- | --- |
| `pexpect` | Interactive prompts, expect/send flows | Higher-level, Unix-focused |
| `pty` | Precise pseudo-terminal semantics | More control, more boilerplate |
| `subprocess` | Non-interactive commands | Not a real terminal |
| platform API | Windows ConPTY or special shells | More platform-specific code |

Document why the chosen backend matches the contract.

3. Define shell configuration.
   Be explicit about shell executable, interactive/login flags, startup files,
   working directory, environment, echo, dimensions, newline behavior, and UTF-8
   handling.

4. Implement lifecycle.
   Cover spawn, liveness checks, send input, read output, timeout, Ctrl+C or
   signal cancellation, process termination, and cleanup after unexpected exit.

5. Test behavior through the public interface.
   Avoid testing private implementation details. Verify state persistence, prompt
   handling, long-running commands, cancellation, non-ASCII output, and cleanup.

## Test Checklist

- Basic command output is captured.
- Environment and working directory behave as expected.
- Shell state persists when the contract requires it.
- Interactive prompts can be answered.
- Ctrl+C or cancellation stops the foreground command without corrupting the
  session.
- Timeouts are deterministic and produce clear errors.
- Dead sessions are detected before writes.
- Cleanup terminates child processes.

## Common Pitfalls

- Using `subprocess` for behavior that needs a real TTY.
- Assuming Bash semantics on Windows or PowerShell semantics on Unix.
- Depending on prompts that vary by user config.
- Reading too early and missing buffered output.
- Letting a hung command block the agent forever.
- Adding unrequested helper APIs instead of satisfying the interface.

## Done Criteria

- The backend choice and shell flags are documented.
- Required interface behavior is covered by focused tests.
- Timeout and cancellation paths are tested.
- Resource cleanup works after success, failure, and cancellation.
