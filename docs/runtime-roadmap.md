# Runtime Roadmap

This project has completed the first productization slice: sessions, resume/fork,
workspace snapshots, safe patching, rollback, permissions, events, slash
commands, and shared profile guardrails.

## Tool Runtime

Current state:

- Tools run through `ToolContext` for workspace, permission policy, events, and
  session identity.
- Built-in tools are registered through a thin `ToolRegistry`.
- Legacy `TOOL_SCHEMAS` and `TOOL_DISPATCH` exports remain for compatibility.

Remaining work:

- Introduce a typed `ToolResult` instead of returning plain strings everywhere.
- Let profiles register additional tools through the registry without editing
  global tool tables.
- Keep the registry boundary ready for future MCP connector tools.

## Observability

Current state:

- Session events are written as JSONL.
- Tool execution records before/after events, permission decisions, approval
  requests, approval results, and file changes.

Remaining work:

- Add a human-readable run summary for each session.
- Add failure classification for common stop states and tool errors.
- Record context compaction and final report events in a consistent format.
- Make session history easier to inspect without reading raw JSONL.
