import { describe, expect, test } from "bun:test";
import { nerdIcons, resolveIcons, unicodeIcons } from "./icons.ts";
import { initialState, reduceEvent } from "./state.ts";
import { resolveTheme } from "./theme.ts";

describe("OpenTUI state", () => {
  test("session reset replaces transcript and clears transient interaction state", () => {
    const interacting = reduceEvent(initialState, { type: "interaction", id: "approval-1", kind: "approval", payload: { toolName: "write_file", args: {}, risk: "edit", reason: "confirm", persistAvailable: false } });
    const reset = reduceEvent(interacting, { type: "session_reset", snapshot: { ...initialState.snapshot, sessionId: "new-session" }, items: [{ id: "old", kind: "assistant", title: "助手", body: "restored" }] });
    expect(reset.interaction).toBeNull();
    expect(reset.items.map((item) => item.body)).toEqual(["restored"]);
    expect(reset.snapshot.sessionId).toBe("new-session");
  });
  test("auto theme follows detected terminal mode", () => {
    expect(resolveTheme("auto", "light").mode).toBe("light");
    expect(resolveTheme("dark", "light").mode).toBe("dark");
  });
  test("auto icons are safe unicode and nerd icons are opt-in", () => {
    expect(resolveIcons("auto")).toBe(unicodeIcons);
    expect(resolveIcons("nerd")).toBe(nerdIcons);
  });
});
