import React from "react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import type { TestRendererOptions, TestRendererSetup } from "@opentui/core/testing";
import { testRender } from "@opentui/react/test-utils";
import { App } from "./app.tsx";
import type { ActionName, ActionResult, UiEvent } from "./protocol.ts";

const renderers: Array<{ destroy: () => void }> = [];
afterEach(() => { while (renderers.length) renderers.pop()?.destroy(); });

async function renderUi(node: ReactNode, options: TestRendererOptions): Promise<TestRendererSetup> {
  const setup = await testRender(node, options);
  // OpenTUI's renderer has explicit flush/wait primitives; subsequent input is
  // not driven by React DOM's act() scheduler.
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
  return setup;
}

async function flush(setup: TestRendererSetup): Promise<void> {
  await setup.flush();
}

async function waitFrame(setup: TestRendererSetup, predicate: (frame: string) => boolean): Promise<string> {
  let frame = "";
  frame = await setup.waitForFrame(predicate);
  return frame;
}

async function waitFor(setup: TestRendererSetup, predicate: () => boolean): Promise<void> {
  await setup.waitFor(predicate);
}

function events(): AsyncIterable<UiEvent> {
  return {
    async *[Symbol.asyncIterator]() {
      yield { type: "snapshot", snapshot: { profile: "general", permissionMode: "workspace-write", model: "deepseek-v4-flash", provider: "auto", contextPercent: 91, status: "ready", cwd: "C:/workspace/project", sessionId: "session-123" } };
      yield { type: "commands", commands: Array.from({ length: 12 }, (_, index) => ({ name: `/command-${index + 1}`, description: `命令 ${index + 1}` })) };
    },
  };
}

describe("OpenTUI app", () => {
  test("enter submits the composer while shift-enter inserts a newline", async () => {
    const submitted: string[] = [];
    const setup = await renderUi(<App onSubmit={(text) => submitted.push(text)} />, { width: 120, height: 24, kittyKeyboard: true });
    renderers.push(setup.renderer);
    await setup.mockInput.typeText("第一行");
    setup.mockInput.pressEnter({ shift: true });
    await setup.mockInput.typeText("第二行");
    await flush(setup);
    expect(submitted).toHaveLength(0);
    setup.mockInput.pressEnter();
    await waitFor(setup, () => submitted.length === 1);
    expect(submitted[0]).toBe("第一行\n第二行");
  });

  test("enter accepts a command completion without submitting the stale slash", async () => {
    const submitted: string[] = [];
    const commandEvents: AsyncIterable<UiEvent> = { async *[Symbol.asyncIterator]() {
      yield { type: "commands", commands: [{ name: "/handoff", description: "生成交接文档" }] };
    } };
    const setup = await renderUi(<App events={commandEvents} onSubmit={(text) => submitted.push(text)} />, { width: 120, height: 24 });
    renderers.push(setup.renderer);
    await setup.mockInput.typeText("/");
    await waitFrame(setup, (frame) => frame.includes("/handoff"));
    setup.mockInput.pressEnter();
    await flush(setup);
    expect(submitted).toHaveLength(0);
    expect(setup.captureCharFrame()).toContain("/handoff");
    setup.mockInput.pressEnter();
    await waitFor(setup, () => submitted.length === 1);
    expect(submitted[0]).toBe("/handoff");
  });

  test("history shortcut opens a searchable panel and escape restores composer", async () => {
    const actions: ActionName[] = [];
    const onAction = async (name: ActionName): Promise<ActionResult> => {
      actions.push(name);
      return { ok: true, panel: { kind: "sessions", title: "历史会话", searchable: true, options: [{ id: "one", label: "修复滚动", description: "刚刚" }] } };
    };
    const setup = await renderUi(<App events={events()} onAction={onAction} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await flush(setup);
    setup.mockInput.pressKey("r", { ctrl: true });
    await waitFrame(setup, (frame) => frame.includes("历史会话"));
    expect(actions).toContain("open_sessions");
    setup.mockInput.pressEscape();
    await waitFrame(setup, (frame) => frame.includes("输入任务") && !frame.includes("搜索会话"));
  });

  test("question-mark opens help without leaking into the composer draft", async () => {
    const submitted: string[] = [];
    const setup = await renderUi(
      <App
        onSubmit={(text) => submitted.push(text)}
        onAction={async () => ({ ok: true, panel: { kind: "help", title: "快捷键与命令", body: "帮助内容" } })}
      />,
      { width: 120, height: 24 },
    );
    renderers.push(setup.renderer);
    setup.mockInput.pressKey("?");
    await waitFrame(setup, (frame) => frame.includes("帮助内容"));
    setup.mockInput.pressEscape();
    await waitFrame(setup, (frame) => frame.includes("输入任务"));
    await setup.mockInput.typeText("测试帮助后输入");
    await flush(setup);
    setup.mockInput.pressEnter();
    await waitFor(setup, () => submitted.length === 1);
    expect(submitted[0]).toBe("测试帮助后输入");
  });

  test("ctrl-n starts a new session and clears the current draft", async () => {
    const actions: ActionName[] = [];
    const setup = await renderUi(
      <App onAction={async (name) => { actions.push(name); return { ok: true, message: "已开始新会话" }; }} />,
      { width: 120, height: 24 },
    );
    renderers.push(setup.renderer);
    await setup.mockInput.typeText("尚未提交的草稿");
    setup.mockInput.pressKey("n", { ctrl: true });
    await waitFor(setup, () => actions.includes("new_session"));
    await waitFrame(setup, (frame) => frame.includes("输入任务") && !frame.includes("尚未提交的草稿"));
  });

  test("ctrl-c cancels a running turn and exits only while idle", async () => {
    let cancelled = 0;
    let exited = 0;
    const runningEvents: AsyncIterable<UiEvent> = { async *[Symbol.asyncIterator]() {
      yield { type: "turn_state", state: "running" };
    } };
    const runningSetup = await renderUi(<App events={runningEvents} onCancel={() => cancelled++} onExit={() => exited++} />, { width: 80, height: 24 });
    renderers.push(runningSetup.renderer);
    await waitFrame(runningSetup, (frame) => frame.includes("运行中"));
    runningSetup.mockInput.pressCtrlC();
    await waitFor(runningSetup, () => cancelled === 1);
    expect(exited).toBe(0);

    const idleSetup = await renderUi(<App onCancel={() => cancelled++} onExit={() => exited++} />, { width: 80, height: 24 });
    renderers.push(idleSetup.renderer);
    await flush(idleSetup);
    idleSetup.mockInput.pressCtrlC();
    await waitFor(idleSetup, () => exited === 1);
    expect(cancelled).toBe(1);
  });

  test("command selection remains visible after scrolling beyond eight rows", async () => {
    const setup = await renderUi(<App events={events()} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await flush(setup);
    await setup.mockInput.typeText("/");
    await waitFrame(setup, (value) => value.includes("1/12"));
    await setup.mockInput.pressKeys(Array.from({ length: 9 }, () => "ARROW_DOWN"), 1);
    const frame = await waitFrame(setup, (value) => value.includes("10/12"));
    expect(frame).toContain("/command-10");
  });

  test("each command stays on one left-aligned row and truncates overflow with an ellipsis", async () => {
    const longCommand = "/third-party-skill-with-an-extremely-long-command-name";
    const longDescription = "这是一个来自第三方技能的特别长说明，用来验证说明文字不会换到第二行，而是在终端剩余宽度内明确显示截断符号";
    const commandEvents: AsyncIterable<UiEvent> = { async *[Symbol.asyncIterator]() {
      yield { type: "commands", commands: [
        { name: "/profile", description: "选择工作模式" },
        { name: "/improve-codebase-architecture", description: "扫描代码库中的架构深化机会" },
        { name: longCommand, description: longDescription },
      ] };
    } };
    const setup = await renderUi(<App events={commandEvents} />, { width: 120, height: 24 });
    renderers.push(setup.renderer);
    await setup.mockInput.typeText("/");
    const truncated = `${longCommand.slice(0, 29)}…`;
    const frame = await waitFrame(setup, (value) => value.includes(truncated) && value.includes("…"));
    const lines = frame.split("\n");
    const shortLine = lines.find((line) => line.includes("/profile")) ?? "";
    const longLine = lines.find((line) => line.includes("/improve-codebase-architecture")) ?? "";
    const truncatedLine = lines.find((line) => line.includes(truncated)) ?? "";
    expect(shortLine.indexOf("/profile")).toBe(longLine.indexOf("/improve-codebase-architecture"));
    expect(shortLine.indexOf("/profile")).toBe(truncatedLine.indexOf(truncated));
    expect(frame).not.toContain(longCommand);
    expect(frame).not.toContain(longDescription);
    expect(truncatedLine.match(/…/g)?.length).toBe(2);
  });

  test("responsive footer hides model at 80 columns and restores it after resize", async () => {
    const setup = await renderUi(<App events={events()} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await waitFrame(setup, (frame) => frame.includes("91% 上下文"));
    expect(setup.captureCharFrame()).not.toContain("deepseek-v4-flash");
    setup.resize(120, 36);
    await waitFrame(setup, (frame) => frame.includes("deepseek-v4-flash"));
  });

  test("top-right history action is clickable", async () => {
    const actions: ActionName[] = [];
    const setup = await renderUi(<App onAction={async (name) => { actions.push(name); return { ok: true, panel: { kind: "sessions", title: "历史会话" } }; }} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await flush(setup);
    await setup.mockMouse.click(75, 0);
    await waitFor(setup, () => actions.length > 0);
    expect(actions[0]).toBe("open_sessions");
  });

  test("approval interaction resolves the selected decision", async () => {
    const resolved: Record<string, unknown>[] = [];
    const interactionEvents: AsyncIterable<UiEvent> = { async *[Symbol.asyncIterator]() {
      yield { type: "interaction", id: "approval-1", kind: "approval", payload: { toolName: "run_bash", args: { command: "git status" }, risk: "shell_risky", reason: "需要确认", persistAvailable: true } };
    } };
    const setup = await renderUi(<App events={interactionEvents} onResolveInteraction={(_id, result) => resolved.push(result)} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await waitFrame(setup, (frame) => frame.includes("需要确认"));
    setup.mockInput.pressArrow("right");
    await setup.flush();
    setup.mockInput.pressEnter();
    await setup.flush();
    await waitFor(setup, () => resolved.length > 0);
    expect(resolved[0].decision).toBe("persist");
  });

  test("question interaction submits the selected option", async () => {
    const resolved: Record<string, unknown>[] = [];
    const questionEvents: AsyncIterable<UiEvent> = { async *[Symbol.asyncIterator]() {
      yield { type: "interaction", id: "question-1", kind: "question", payload: { question: "选择框架", options: [
        { label: "React", value: "react", description: "组件化界面", is_other: false },
        { label: "其他", value: "other", description: "输入自定义答案", is_other: true },
      ] } };
    } };
    const setup = await renderUi(<App events={questionEvents} onResolveInteraction={(_id, result) => resolved.push(result)} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await waitFrame(setup, (frame) => frame.includes("选择框架"));
    setup.mockInput.pressArrow("down");
    await setup.flush();
    setup.mockInput.pressEnter();
    await setup.flush();
    await waitFor(setup, () => resolved.length > 0);
    expect(resolved[0].selectedIndex).toBe(1);
  });

  test("first frame does not wait for a slow Python event stream", async () => {
    const slowEvents: AsyncIterable<UiEvent> = { async *[Symbol.asyncIterator]() {
      await new Promise((resolve) => setTimeout(resolve, 500));
      yield { type: "progress", status: "ready", detail: "late ready" };
    } };
    const started = performance.now();
    const setup = await renderUi(<App events={slowEvents} />, { width: 80, height: 24 });
    renderers.push(setup.renderer);
    await waitFrame(setup, (frame) => frame.includes("VeriForge"));
    expect(performance.now() - started).toBeLessThan(200);
  });
});
