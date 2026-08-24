import React from "react";
import { CliRenderEvents, createCliRenderer } from "@opentui/core";
import { createRoot } from "@opentui/react";
import { App } from "./app.tsx";
import { ActionName, ActionResult, BridgeMessage, BridgeRequest, DEFAULT_COMMANDS, IconPreference, ThemePreference, UiEvent } from "./protocol.ts";

function flag(name: string): boolean {
  return process.argv.includes(name);
}

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  return process.argv[index + 1];
}

function mockEvents(): AsyncIterable<UiEvent> {
  return {
    async *[Symbol.asyncIterator]() {
      yield { type: "progress", status: "ready", detail: "界面就绪" };
      yield {
        type: "commands",
        commands: [
          ...DEFAULT_COMMANDS,
          { name: "/handoff", category: "Skills", description: "整理当前上下文并生成交接文档" },
          { name: "/implement", category: "Skills", description: "按计划执行实现任务" },
          { name: "/triage", category: "Skills", description: "整理问题并生成可执行简报" },
          { name: "/workflows", category: "Skills", description: "查看当前可用工作流" },
        ],
      };
      yield { type: "turn_state", state: "idle" };
      await new Promise((resolve) => setTimeout(resolve, 150));
      yield { type: "notice", text: "OpenTUI 已就绪：输入 / 查看命令，Ctrl+R 打开历史会话。" };
    },
  };
}

type BridgeClient = {
  events: AsyncIterable<UiEvent>;
  closed: Promise<void>;
  submit: (text: string) => void;
  cancel: () => void;
  action: (name: ActionName, params?: Record<string, unknown>) => Promise<ActionResult>;
  resolveInteraction: (id: string, result: Record<string, unknown>) => void;
  shutdown: () => void;
};

function createBridgeClient(): BridgeClient {
  const python = option("--python") ?? "python";
  const cwd = option("--cwd") ?? process.cwd();
  const profile = option("--profile") ?? "general";
  const args = [
    python,
    "-m",
    "harness_code_agent.tui_bridge",
    "--cwd",
    cwd,
    "--profile",
    profile,
  ];
  if (flag("--profile-explicit")) args.push("--profile-explicit");

  const child = Bun.spawn(args, {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "inherit",
  });
  let requestCounter = 0;
  let ended = false;
  let buffer = "";
  const pending = new Map<string, { resolve: (value: unknown) => void; reject: (reason: unknown) => void }>();
  const eventQueue: UiEvent[] = [];
  const eventWaiters: Array<(event: UiEvent | null) => void> = [];
  let resolveClosed: () => void = () => undefined;
  const closed = new Promise<void>((resolve) => { resolveClosed = resolve; });

  const enqueue = (event: UiEvent) => {
    const waiter = eventWaiters.shift();
    if (waiter) waiter(event);
    else eventQueue.push(event);
  };

  const consumeMessage = (message: BridgeMessage) => {
    if (message.type === "event") {
      enqueue(message.event);
      return;
    }
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.ok) waiter.resolve(message.result);
    else waiter.reject(new Error(message.error));
  };

  const readLoop = async () => {
    if (!child.stdout) return;
    const decoder = new TextDecoder();
    try {
      for await (const chunk of child.stdout) {
        buffer += decoder.decode(chunk, { stream: true });
        let newline = buffer.indexOf("\n");
        while (newline >= 0) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) {
            try {
              consumeMessage(JSON.parse(line) as BridgeMessage);
            } catch (error) {
              console.error(`OpenTUI bridge sent invalid JSON: ${String(error)}`);
            }
          }
          newline = buffer.indexOf("\n");
        }
      }
    } finally {
      ended = true;
      for (const waiter of pending.values()) waiter.reject(new Error("OpenTUI bridge exited"));
      pending.clear();
      while (eventWaiters.length) eventWaiters.shift()?.(null);
      resolveClosed();
    }
  };
  void readLoop();

  const request = (method: BridgeRequest["method"], params?: Record<string, unknown>) => {
    if (ended || !child.stdin) return Promise.reject(new Error("OpenTUI bridge is closed"));
    const id = `req-${++requestCounter}`;
    const message: BridgeRequest = { type: "request", id, method, params };
    child.stdin.write(`${JSON.stringify(message)}\n`);
    child.stdin.flush();
    return new Promise<unknown>((resolve, reject) => pending.set(id, { resolve, reject }));
  };

  void request("initialize").catch((error) => console.error(`OpenTUI bridge initialization failed: ${String(error)}`));

  const events: AsyncIterable<UiEvent> = {
    async *[Symbol.asyncIterator]() {
      while (true) {
        if (eventQueue.length) {
          yield eventQueue.shift() as UiEvent;
          continue;
        }
        if (ended) return;
        const event = await new Promise<UiEvent | null>((resolve) => eventWaiters.push(resolve));
        if (event === null) return;
        yield event;
      }
    },
  };

  const fire = (method: BridgeRequest["method"], params?: Record<string, unknown>) => {
    void request(method, params).catch((error) => console.error(`OpenTUI bridge ${method} failed: ${String(error)}`));
  };
  const shutdown = () => {
    if (!ended) fire("shutdown");
  };
  process.once("exit", () => {
    if (!ended) child.kill();
  });

  return {
    events,
    closed,
    submit: (text) => fire("submit", { text }),
    cancel: () => fire("cancel"),
    action: (name, params) => request("action", { name, params }).then((result) => result as ActionResult),
    resolveInteraction: (id, result) => fire("resolve_interaction", { id, result }),
    shutdown,
  };
}

function mockAction(name: ActionName): Promise<ActionResult> {
  if (name === "open_sessions") return Promise.resolve({ ok: true, panel: { kind: "sessions", title: "历史会话", searchable: true, options: [
    { id: "session-1", label: "重构命令栏滚动与焦点", description: "coding-agent · 刚刚" },
    { id: "session-2", label: "检查 MCP 工具加载", description: "general · 2 小时前" },
  ], footer: "Enter 恢复 · Esc 关闭" } });
  if (name === "open_panel") return Promise.resolve({ ok: true, panel: { kind: "help", title: "快捷键与命令", body: "Ctrl+R 历史 · Ctrl+N 新会话 · Ctrl+O 运行观察" } });
  if (name === "complete_mention") return Promise.resolve({ ok: true, candidates: [{ insertText: "file:README.md", display: "@file:README.md", description: "file" }] });
  return Promise.resolve({ ok: true, message: "操作已完成" });
}

async function main() {
  const noAltScreen = flag("--no-alt-screen");
  const mock = flag("--mock");
  const bridge = flag("--bridge");
  const client = bridge ? createBridgeClient() : undefined;
  const initialTask = option("--first-task");
  const themePreference = (option("--theme") ?? "auto") as ThemePreference;
  const iconPreference = (option("--icons") ?? "auto") as IconPreference;
  const renderer = await createCliRenderer({
    exitOnCtrlC: false,
    clearOnShutdown: true,
    screenMode: noAltScreen ? "main-screen" : "alternate-screen",
  });
  renderer.on(CliRenderEvents.FOCUS, () => renderer.requestRender());
  let rendererDestroyed = false;
  const destroyRenderer = () => {
    if (rendererDestroyed) return;
    rendererDestroyed = true;
    renderer.destroy();
  };
  createRoot(renderer).render(
    <App
      events={client?.events ?? (mock ? mockEvents() : undefined)}
      initialTask={initialTask}
      onSubmit={client?.submit ?? ((text) => { if (mock) console.error(`mock submit: ${text}`); })}
      onCancel={client?.cancel ?? (() => { if (mock) console.error("mock cancel"); })}
      onAction={client?.action ?? (mock ? mockAction : undefined)}
      onResolveInteraction={client?.resolveInteraction}
      themePreference={themePreference}
      iconPreference={iconPreference}
      onExit={() => {
        client?.shutdown();
        destroyRenderer();
      }}
    />,
  );
  void client?.closed.then(destroyRenderer);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
