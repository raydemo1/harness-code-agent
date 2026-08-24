export type ThemePreference = "auto" | "dark" | "light";
export type IconPreference = "auto" | "nerd" | "unicode";

export type Snapshot = {
  profile: string;
  permissionMode: string;
  model: string;
  provider: string;
  contextPercent: number;
  status: string;
  cwd: string;
  sessionId?: string;
  routingMode?: "auto" | "pinned";
  dirtyCount?: number;
};

export type CommandItem = { name: string; description: string; category?: string };
export type TranscriptItem = {
  id: string;
  kind: "user" | "assistant" | "tool" | "status" | "plan" | "error" | "file" | "thought" | "profile";
  title: string;
  body: string;
  state?: "running" | "success" | "failed" | "pending" | "changed";
};

export type ApprovalInteraction = {
  type: "interaction";
  id: string;
  kind: "approval";
  payload: { toolName: string; args: Record<string, unknown>; risk: string; reason: string; persistAvailable: boolean };
};
export type QuestionInteraction = {
  type: "interaction";
  id: string;
  kind: "question";
  payload: { question: string; options: Array<{ label: string; value: string; description: string; is_other: boolean }> };
};
export type Interaction = ApprovalInteraction | QuestionInteraction;

export type PanelOption = { id: string; label: string; description?: string; tone?: "default" | "success" | "warning" | "danger" };
export type PanelSpec = {
  kind: "sessions" | "profile" | "checkpoint" | "mcp" | "observe" | "help";
  title: string;
  body?: string;
  options?: PanelOption[];
  footer?: string;
  searchable?: boolean;
};

export type ActionName = "open_sessions" | "new_session" | "open_panel" | "panel_action" | "toggle_permission" | "complete_mention";
export type ActionResult = {
  ok: boolean;
  message?: string;
  panel?: PanelSpec;
  candidates?: Array<{ insertText: string; display: string; description: string }>;
};

export type UiEvent =
  | { type: "snapshot"; snapshot: Snapshot }
  | { type: "session_reset"; snapshot: Snapshot; items?: TranscriptItem[] }
  | { type: "transcript"; item: TranscriptItem }
  | { type: "transcript_update"; id: string; body: string; state?: TranscriptItem["state"] }
  | { type: "assistant_delta"; id: string; text: string }
  | { type: "commands"; commands: CommandItem[] }
  | { type: "progress"; status: string; detail: string }
  | { type: "notice"; text: string; level?: "info" | "warning" | "error" }
  | { type: "turn_state"; state: "idle" | "running" | "queued" | "cancelled"; queueDepth?: number }
  | { type: "panel"; panel: PanelSpec }
  | Interaction
  | { type: "interaction_closed"; id: string }
  | { type: "shutdown"; reason?: string };

export type BridgeRequest = {
  type: "request";
  id: string;
  method: "initialize" | "submit" | "cancel" | "action" | "resolve_interaction" | "shutdown";
  params?: Record<string, unknown>;
};
export type BridgeMessage =
  | { type: "response"; id: string; ok: true; result?: unknown }
  | { type: "response"; id: string; ok: false; error: string }
  | { type: "event"; event: UiEvent };

export const DEFAULT_COMMANDS: CommandItem[] = [
  { name: "/profile", category: "工作流", description: "选择工作模式并固定后续路由" },
  { name: "/checkpoint", category: "工作流", description: "打开检查点管理" },
  { name: "/mcp", category: "工作流", description: "打开 MCP 服务与工具管理" },
  { name: "/compact", category: "工作流", description: "压缩当前对话上下文" },
  { name: "/fork", category: "会话", description: "从当前会话创建并进入分支" },
  { name: "/observe", category: "会话", description: "打开当前项目的运行观察" },
];
