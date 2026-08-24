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
