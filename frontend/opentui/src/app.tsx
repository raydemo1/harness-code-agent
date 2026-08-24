import React, { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { stringWidth } from "bun";
import { CliRenderEvents } from "@opentui/core";
import type { ScrollBoxRenderable, TextareaRenderable } from "@opentui/core";
import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/react";
import { resolveIcons } from "./icons.ts";
import type { IconSet } from "./icons.ts";
import type { ActionName, ActionResult, CommandItem, IconPreference, Interaction, PanelSpec, ThemePreference, TranscriptItem, UiEvent } from "./protocol.ts";
import { initialState, reduceEvent, withOptimisticUserMessage } from "./state.ts";
import { resolveTheme } from "./theme.ts";
import type { Theme, ThemeMode } from "./theme.ts";

type AppProps = {
  events?: AsyncIterable<UiEvent>;
  onSubmit?: (text: string) => void;
  onCancel?: () => void;
  onExit?: () => void;
  onAction?: (name: ActionName, params?: Record<string, unknown>) => Promise<ActionResult>;
  onResolveInteraction?: (id: string, result: Record<string, unknown>) => void;
  initialTask?: string;
  themePreference?: ThemePreference;
  iconPreference?: IconPreference;
};

function isEnterKey(name: string): boolean {
  return name === "return" || name === "kpenter" || name === "enter";
}

function commandMatches(command: CommandItem, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  const haystack = `${command.name} ${command.description}`.toLowerCase();
  if (haystack.startsWith(normalized) || command.name.toLowerCase().startsWith(normalized)) return true;
  let cursor = 0;
  for (const char of normalized) {
    cursor = haystack.indexOf(char, cursor);
    if (cursor < 0) return false;
    cursor += 1;
  }
  return true;
}

function markerFor(item: TranscriptItem, icons: IconSet): string {
  if (item.state === "success") return icons.success;
  if (item.state === "failed") return icons.failure;
  if (item.state === "pending") return icons.pending;
  if (item.kind === "file") return icons.file;
  if (item.kind === "tool") return icons.tool;
  if (item.kind === "profile") return icons.profile;
  if (item.kind === "thought") return icons.question;
  return icons.running;
}

function toneFor(item: TranscriptItem, theme: Theme): string {
  if (item.state === "failed" || item.kind === "error") return theme.error;
  if (item.state === "success") return theme.success;
  if (item.state === "pending" || item.kind === "plan") return theme.warning;
  return theme.accent;
}

function Transcript({ items, theme, icons }: { items: TranscriptItem[]; theme: Theme; icons: IconSet }) {
  return (
    <scrollbox stickyScroll focused style={{ height: 1, flexGrow: 1, flexShrink: 1, minHeight: 0, paddingLeft: 2, paddingRight: 2 }}>
      <box style={{ flexDirection: "column", gap: 1, paddingTop: 1, paddingBottom: 1 }}>
        {items.map((item) => {
          if (item.kind === "user" || item.kind === "assistant") {
            const assistant = item.kind === "assistant";
            return (
              <box key={item.id} style={{ flexDirection: "column", maxWidth: 96 }}>
                <text fg={assistant ? theme.success : theme.accent}>
                  <strong>{`${assistant ? icons.assistant : icons.prompt} ${assistant ? "助手" : "你"}`}</strong>
                </text>
                <text fg={theme.text}>{item.body}</text>
              </box>
            );
          }
          if (item.kind === "file" && item.body) {
            return (
              <box key={item.id} style={{ flexDirection: "column", maxWidth: 110 }}>
                <text fg={theme.diffAdd}>{`  ${icons.file} ${item.title}`}</text>
                <text fg={theme.muted}>{item.body}</text>
              </box>
            );
          }
          return (
            <box key={item.id} style={{ flexDirection: "column", maxWidth: 110 }}>
              <text fg={toneFor(item, theme)}>{`  ${markerFor(item, icons)} ${item.title}${item.body ? `  ${item.body}` : ""}`}</text>
            </box>
          );
        })}
      </box>
    </scrollbox>
  );
}

function IconAction({ icon, label, shortcut, theme, onInvoke }: { icon: string; label: string; shortcut: string; theme: Theme; onInvoke: () => void }) {
  const [hovered, setHovered] = useState(false);
  return (
    <box
      focusable
      onMouseOver={() => setHovered(true)}
      onMouseOut={() => setHovered(false)}
      onMouseDown={onInvoke}
      onKeyDown={(key) => { if (isEnterKey(key.name) || key.name === "space") onInvoke(); }}
      style={{ flexDirection: "row", paddingLeft: 1, paddingRight: 1, backgroundColor: hovered ? theme.surfaceSelected : theme.background }}
    >
      <text fg={hovered ? theme.focus : theme.muted}>{icon}</text>
      {hovered ? <text fg={theme.subtle}>{` ${label} ${shortcut}`}</text> : null}
    </box>
  );
}

function Header({ cwd, theme, icons, compact, onHistory, onNew }: { cwd: string; theme: Theme; icons: IconSet; compact: boolean; onHistory: () => void; onNew: () => void }) {
  const label = compact ? (cwd.replace(/\\/g, "/").split("/").filter(Boolean).at(-1) ?? cwd) : cwd;
  return (
    <box style={{ flexDirection: "row", justifyContent: "space-between", height: 1, flexShrink: 0, paddingLeft: 2, paddingRight: 1 }}>
      <text fg={theme.subtle}>{label}</text>
      <box style={{ flexDirection: "row" }}>
        <IconAction icon={icons.session} label="历史" shortcut="Ctrl+R" theme={theme} onInvoke={onHistory} />
        <IconAction icon={icons.newSession} label="新会话" shortcut="Ctrl+N" theme={theme} onInvoke={onNew} />
      </box>
    </box>
  );
}

function PanelView({ panel, theme, icons, onClose, onSelect }: { panel: PanelSpec; theme: Theme; icons: IconSet; onClose: () => void; onSelect: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const scrollRef = useRef<ScrollBoxRenderable | null>(null);
  const options = useMemo(() => (panel.options ?? []).filter((item) => `${item.label} ${item.description ?? ""}`.toLowerCase().includes(query.toLowerCase())), [panel.options, query]);
  useEffect(() => setSelected((value) => Math.min(value, Math.max(0, options.length - 1))), [options.length]);
  useEffect(() => scrollRef.current?.scrollChildIntoView(`panel-option-${selected}`), [selected]);
  useKeyboard((key) => {
    if (key.name === "escape") onClose();
    else if (key.name === "up" && options.length) setSelected((value) => (value - 1 + options.length) % options.length);
    else if (key.name === "down" && options.length) setSelected((value) => (value + 1) % options.length);
    else if (key.name === "pageup") setSelected((value) => Math.max(0, value - 8));
    else if (key.name === "pagedown") setSelected((value) => Math.min(options.length - 1, value + 8));
    else if (isEnterKey(key.name) && options[selected]) onSelect(options[selected].id);
  });
  return (
    <box style={{ flexDirection: "column", height: 1, flexGrow: 1, flexShrink: 1, minHeight: 0, marginLeft: 2, marginRight: 2, backgroundColor: theme.surface }}>
      <box style={{ flexDirection: "row", justifyContent: "space-between", height: 1, paddingLeft: 1, paddingRight: 1, backgroundColor: theme.surfaceRaised }}>
        <text fg={theme.accent}><strong>{`${panel.kind === "sessions" ? icons.session : panel.kind === "observe" ? icons.observe : icons.profile} ${panel.title}`}</strong></text>
        <text fg={theme.subtle}>{`${icons.close} Esc`}</text>
      </box>
      {panel.searchable ? <input value={query} placeholder={`${icons.search} 搜索会话…`} focused onInput={setQuery} style={{ paddingLeft: 1, paddingRight: 1, backgroundColor: theme.surface, textColor: theme.text, cursorColor: theme.accent, placeholderColor: theme.muted }} /> : null}
      {panel.body ? <scrollbox style={{ maxHeight: options.length ? 10 : "100%", paddingLeft: 1, paddingRight: 1 }}><text fg={theme.muted}>{panel.body}</text></scrollbox> : null}
      {options.length ? (
        <scrollbox ref={scrollRef} style={{ flexGrow: 1, minHeight: 1, paddingTop: 1, paddingBottom: 1 }}>
          {options.map((option, index) => {
            const active = index === selected;
            const tone = option.tone === "danger" ? theme.error : option.tone === "warning" ? theme.warning : option.tone === "success" ? theme.success : active ? theme.focus : theme.text;
            return (
              <box id={`panel-option-${index}`} key={option.id} onMouseDown={() => onSelect(option.id)} style={{ flexDirection: "column", paddingLeft: 1, paddingRight: 1, backgroundColor: active ? theme.surfaceSelected : theme.surface }}>
                <text fg={tone}>{`${active ? icons.prompt : " "} ${option.label}`}</text>
                {option.description ? <text fg={theme.subtle}>{`    ${option.description}`}</text> : null}
              </box>
            );
          })}
        </scrollbox>
      ) : null}
      {panel.footer ? <text fg={theme.subtle}>{` ${panel.footer}`}</text> : null}
    </box>
  );
}

function InteractionView({ interaction, theme, icons, onResolve }: { interaction: Interaction; theme: Theme; icons: IconSet; onResolve: (result: Record<string, unknown>) => void }) {
  const [selected, setSelected] = useState(0);
  const selectedRef = useRef(0);
  const [customText, setCustomText] = useState("");
  const approvalOptions = interaction.kind === "approval"
    ? [{ label: "仅本次允许", value: "approve" }, ...(interaction.payload.persistAvailable ? [{ label: "信任此前缀", value: "persist" }] : []), { label: "拒绝", value: "deny" }]
    : [];
  const questionOptions = interaction.kind === "question" ? interaction.payload.options : [];
  const options = interaction.kind === "approval" ? approvalOptions : questionOptions.map((option, index) => ({ ...option, label: option.label, value: String(index) }));
  const select = (index: number) => {
    selectedRef.current = index;
    setSelected(index);
  };
  useKeyboard((key) => {
    if (key.name === "escape") onResolve(interaction.kind === "approval" ? { decision: "deny" } : { cancelled: true });
    else if (key.name === "left" || key.name === "up") select((selectedRef.current - 1 + options.length) % options.length);
    else if (key.name === "right" || key.name === "down") select((selectedRef.current + 1) % options.length);
    else if (isEnterKey(key.name)) {
      if (interaction.kind === "approval") onResolve({ decision: approvalOptions[selectedRef.current].value });
      else onResolve({ selectedIndex: selectedRef.current, customText });
    }
    else if (/^[1-9]$/.test(key.name)) {
      const index = Number(key.name) - 1;
      if (index < options.length) select(index);
    }
  });
  const otherSelected = interaction.kind === "question" && Boolean(questionOptions[selected]?.is_other);
  return (
    <box style={{ flexDirection: "column", flexShrink: 0, marginLeft: 2, marginRight: 2, paddingLeft: 1, paddingRight: 1, paddingTop: 1, paddingBottom: 1, backgroundColor: theme.surfaceRaised }}>
      <text fg={interaction.kind === "approval" ? theme.warning : theme.accent}><strong>{`${interaction.kind === "approval" ? icons.approval : icons.question} ${interaction.kind === "approval" ? "需要确认" : interaction.payload.question}`}</strong></text>
      {interaction.kind === "approval" ? <text fg={theme.muted}>{`${interaction.payload.toolName} · ${interaction.payload.risk}\n${interaction.payload.reason}\n${JSON.stringify(interaction.payload.args, null, 2)}`}</text> : null}
      <box style={{ flexDirection: "column", paddingTop: 1 }}>
        {options.map((option, index) => <text key={`${option.value}-${index}`} fg={index === selected ? theme.focus : (interaction.kind === "approval" && option.value === "deny" ? theme.error : theme.text)}>{`${index === selected ? icons.prompt : " "} [${index + 1}] ${option.label}${interaction.kind === "question" && questionOptions[index]?.description ? ` — ${questionOptions[index].description}` : ""}`}</text>)}
      </box>
      {otherSelected ? <input value={customText} placeholder="其他说明…" focused onInput={setCustomText} style={{ backgroundColor: theme.surface, textColor: theme.text, cursorColor: theme.accent, placeholderColor: theme.muted }} /> : null}
      <text fg={theme.subtle}>↑↓ 选择 · Enter 确认 · Esc 取消</text>
    </box>
  );
}

type Completion = { id: string; label: string; description: string; insert: string };

const COMMAND_LABEL_WIDTH = 30;

function truncateLabel(label: string, maxWidth: number): string {
  if (label.length <= maxWidth) return label;
  return `${label.slice(0, Math.max(0, maxWidth - 1))}…`;
}

function truncateText(text: string, maxWidth: number): string {
  if (stringWidth(text) <= maxWidth) return text;
  if (maxWidth <= 1) return "…";
  let result = "";
  for (const char of text) {
    if (stringWidth(result + char) > maxWidth - 1) break;
    result += char;
  }
  return `${result.trimEnd()}…`;
}

function Composer({ value, onChange, onSubmit, onCancel, onExit, running, commands, theme, icons, compact, terminalWidth, disabled, onAction }: { value: string; onChange: (value: string) => void; onSubmit: () => void; onCancel: () => void; onExit?: () => void; running: boolean; commands: CommandItem[]; theme: Theme; icons: IconSet; compact: boolean; terminalWidth: number; disabled: boolean; onAction?: AppProps["onAction"] }) {
  const [selected, setSelected] = useState(0);
  const [mentions, setMentions] = useState<Completion[]>([]);
  const editorRef = useRef<TextareaRenderable | null>(null);
  const paletteRef = useRef<ScrollBoxRenderable | null>(null);
  const slashQuery = value.trimStart().startsWith("/") && !value.includes(" ") ? value.trim() : "";
  const mentionMatch = value.match(/(?:^|\s)@([^\s]*)$/);
  useEffect(() => {
    let active = true;
    if (!mentionMatch || !onAction) { setMentions([]); return; }
    void onAction("complete_mention", { prefix: mentionMatch[1] }).then((result) => {
      if (active) setMentions((result.candidates ?? []).map((item) => ({ id: item.insertText, label: item.display, description: item.description, insert: `@${item.insertText} ` })));
    }).catch(() => { if (active) setMentions([]); });
    return () => { active = false; };
  }, [mentionMatch?.[1], onAction]);
  const candidates = useMemo<Completion[]>(() => {
    if (slashQuery) return commands.filter((command) => commandMatches(command, slashQuery)).map((command) => ({ id: command.name, label: command.name, description: command.description, insert: `${command.name} ` }));
    return mentionMatch ? mentions : [];
  }, [commands, slashQuery, mentionMatch?.[1], mentions]);
  const commandMode = Boolean(slashQuery);
  const descriptionWidth = Math.max(1, terminalWidth - COMMAND_LABEL_WIDTH - 8);
  const paletteOpen = !disabled && candidates.length > 0 && Boolean(slashQuery || mentionMatch);
  useEffect(() => setSelected((value) => Math.min(value, Math.max(0, candidates.length - 1))), [candidates.length, slashQuery, mentionMatch?.[1]]);
  useEffect(() => { if (paletteOpen) paletteRef.current?.scrollChildIntoView(`completion-${selected}`); }, [paletteOpen, selected]);
  useEffect(() => { if (editorRef.current && editorRef.current.plainText !== value) editorRef.current.setText(value); }, [value]);
  const applyCompletion = (completion: Completion) => {
    const next = mentionMatch ? value.slice(0, value.lastIndexOf("@")) + completion.insert : completion.insert;
    editorRef.current?.setText(next);
    onChange(next);
  };
  useKeyboard((key) => {
    if (disabled) return;
    if (paletteOpen) {
      let handled = true;
      if (key.name === "up") setSelected((value) => (value - 1 + candidates.length) % candidates.length);
      else if (key.name === "down") setSelected((value) => (value + 1) % candidates.length);
      else if (key.name === "pageup") setSelected((value) => Math.max(0, value - 8));
      else if (key.name === "pagedown") setSelected((value) => Math.min(candidates.length - 1, value + 8));
      else if (key.name === "home") setSelected(0);
      else if (key.name === "end") setSelected(candidates.length - 1);
      else if (key.name === "tab" || isEnterKey(key.name)) applyCompletion(candidates[selected]);
      else if (key.name === "escape") { editorRef.current?.setText(""); onChange(""); }
      else handled = false;
      if (handled) key.preventDefault();
    } else if (key.name === "escape") {
      key.preventDefault();
      onCancel();
    }
  });
  return (
    <box style={{ flexDirection: "column", flexShrink: 0, paddingLeft: 2, paddingRight: 2 }}>
      {paletteOpen ? (
        <box style={{ flexDirection: "column", backgroundColor: theme.surfaceRaised, paddingLeft: 1, paddingRight: 1 }}>
          <scrollbox ref={paletteRef} style={{ height: Math.min(candidates.length, 8), backgroundColor: theme.surfaceRaised }}>
            {candidates.map((item, index) => (
              <box id={`completion-${index}`} key={item.id} style={{ flexDirection: "row", backgroundColor: index === selected ? theme.surfaceSelected : theme.surfaceRaised }}>
                <box style={{ width: commandMode ? COMMAND_LABEL_WIDTH + 2 : 16, flexDirection: "row", flexShrink: 0, justifyContent: "flex-start", paddingLeft: 1, paddingRight: 1 }}>
                  <text fg={index === selected ? theme.focus : theme.accent}>{commandMode ? truncateLabel(item.label, COMMAND_LABEL_WIDTH) : item.label}</text>
                </box>
                {compact ? null : (
                  <text fg={index === selected ? theme.text : theme.subtle} wrapMode="none" truncate>
                    {truncateText(item.description, descriptionWidth)}
                  </text>
                )}
              </box>
            ))}
          </scrollbox>
          <text fg={theme.subtle}>{` ${selected + 1}/${candidates.length} · ↑↓ 选择 · Enter/Tab 使用 · Esc 关闭`}</text>
        </box>
      ) : null}
      <box style={{ flexDirection: "row", backgroundColor: theme.surface, paddingTop: 1, paddingBottom: 1 }}>
        <text fg={theme.accent}><strong>{` ${icons.prompt} `}</strong></text>
        <textarea
          ref={editorRef}
          initialValue={value}
          placeholder="输入任务  ·  / 命令  ·  @ 文件"
          keyBindings={[
            { name: "return", action: "submit" },
            { name: "kpenter", action: "submit" },
            { name: "linefeed", action: "submit" },
            { name: "return", shift: true, action: "newline" },
            { name: "kpenter", shift: true, action: "newline" },
            { name: "linefeed", shift: true, action: "newline" },
          ]}
          onContentChange={() => onChange(editorRef.current?.plainText ?? "")}
          onSubmit={() => { if (!paletteOpen) onSubmit(); }}
          focused={!disabled}
          style={{ flexGrow: 1, minHeight: 3, maxHeight: 7, backgroundColor: theme.surface, textColor: theme.text, cursorColor: theme.accent, placeholderColor: theme.muted }}
        />
      </box>
      <box style={{ flexDirection: "row", justifyContent: "space-between", height: 1 }}>
        <text fg={theme.subtle}>{running ? " Ctrl+C 中断 · 新消息将排队" : " ? 帮助 · Ctrl+R 历史 · Ctrl+N 新会话"}</text>
        <text fg={theme.subtle}>{running ? "运行中" : "Enter 提交 · Shift+Enter 换行"}</text>
      </box>
    </box>
  );
}

function Footer({ theme, snapshot, compact, onPermission }: { theme: Theme; snapshot: typeof initialState.snapshot; compact: boolean; onPermission: () => void }) {
  const permission = snapshot.permissionMode === "workspace-write" ? "工作区可写" : snapshot.permissionMode === "danger-full-access" ? "完全访问" : snapshot.permissionMode;
  return (
    <box style={{ flexDirection: "row", height: 1, flexShrink: 0, paddingLeft: 2, paddingRight: 2 }}>
      <text fg={theme.accent}>{snapshot.profile}</text><text fg={theme.subtle}> · </text>
      <text fg={snapshot.permissionMode === "danger-full-access" ? theme.error : theme.muted} onMouseDown={onPermission}>{permission}</text>
      <text fg={theme.subtle}> · </text><text fg={theme.muted}>{`${snapshot.contextPercent}% 上下文`}</text>
      {!compact ? <><text fg={theme.subtle}> · </text><text fg={theme.text}>{snapshot.model}</text><text fg={theme.subtle}>{snapshot.sessionId ? ` · ${snapshot.sessionId.slice(0, 8)}` : ""}</text></> : null}
    </box>
  );
}

export function App({ events, onSubmit, onCancel, onExit, onAction, onResolveInteraction, initialTask, themePreference = "auto", iconPreference = "auto" }: AppProps) {
  const [state, dispatch] = useReducer(reduceEvent, initialState);
  const [draft, setDraft] = useState("");
  const [composerVersion, setComposerVersion] = useState(0);
  const [panel, setPanel] = useState<PanelSpec | null>(null);
  const [detectedTheme, setDetectedTheme] = useState<ThemeMode | null>(null);
  const renderer = useRenderer();
  const { width } = useTerminalDimensions();
  const theme = resolveTheme(themePreference, detectedTheme);
  const icons = resolveIcons(iconPreference);
  const compact = width < 96;
  const running = state.turnState === "running" || state.turnState === "queued";

  useEffect(() => {
    let active = true;
    void renderer.waitForThemeMode(120).then((mode) => { if (active && mode) setDetectedTheme(mode); });
    const handler = (mode: ThemeMode) => setDetectedTheme(mode);
    renderer.on(CliRenderEvents.THEME_MODE, handler);
    return () => { active = false; renderer.off(CliRenderEvents.THEME_MODE, handler); };
  }, [renderer]);
  useEffect(() => {
    if (!events) return;
    let active = true;
    void (async () => {
      for await (const event of events) {
        if (!active) return;
        if (event.type === "panel") setPanel(event.panel);
        else {
          if (event.type === "session_reset") {
            setPanel(null);
            setDraft("");
            setComposerVersion((version) => version + 1);
          }
          dispatch(event);
        }
      }
    })();
    return () => { active = false; };
  }, [events]);

  const invoke = async (name: ActionName, params?: Record<string, unknown>) => {
    if (!onAction) return;
    try {
      const result = await onAction(name, params);
      if (result.panel) setPanel(result.panel);
      else if (name === "new_session") {
        setPanel(null);
        setDraft("");
        setComposerVersion((version) => version + 1);
      }
      if (result.message) dispatch({ type: "notice", text: result.message });
    } catch (error) {
      dispatch({ type: "notice", level: "error", text: String(error) });
    }
  };

  useKeyboard((key) => {
    const helpKey = key.name === "?" || (key.name === "/" && key.shift);
    if (key.ctrl && key.name === "c") { key.preventDefault(); if (running) onCancel?.(); else onExit?.(); }
    else if (key.ctrl && key.name === "r") { key.preventDefault(); void invoke("open_sessions"); }
    else if (key.ctrl && key.name === "n") { key.preventDefault(); void invoke("new_session"); }
    else if (key.ctrl && key.name === "o") { key.preventDefault(); void invoke("open_panel", { panel: "observe" }); }
    else if (key.ctrl && key.name === "p") { key.preventDefault(); void invoke("toggle_permission"); }
    else if (helpKey && !draft && !panel && !state.interaction) { key.preventDefault(); void invoke("open_panel", { panel: "help" }); }
  });

  useEffect(() => {
    const text = initialTask?.trim();
    if (!text) return;
    dispatch({ type: "transcript", item: { id: `user-${Date.now()}`, kind: "user", title: "你", body: text } });
    onSubmit?.(text);
  }, [initialTask, onSubmit]);

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    const panelCommands: Record<string, string> = { "/profile": "profile", "/checkpoint": "checkpoint", "/mcp": "mcp", "/observe": "observe" };
    if (panelCommands[text]) { setDraft(""); void invoke("open_panel", { panel: panelCommands[text] }); return; }
    if (text === "/compact" || text === "/fork") { setDraft(""); void invoke("panel_action", { panel: "command", action: text.slice(1) }); return; }
    dispatch({ type: "transcript", item: withOptimisticUserMessage(state, text).items.at(-1)! });
    setDraft("");
    onSubmit?.(text);
  };
  const selectPanel = (id: string) => {
    if (!panel) return;
    void (async () => {
      if (!onAction) return;
      try {
        const result = await onAction("panel_action", { panel: panel.kind, action: id });
        if (result.panel) setPanel(result.panel);
        else setPanel(null);
        if (result.message) dispatch({ type: "notice", text: result.message });
      } catch (error) { dispatch({ type: "notice", level: "error", text: String(error) }); }
    })();
  };
  const resolveInteraction = (result: Record<string, unknown>) => {
    const interaction = state.interaction;
    if (!interaction) return;
    onResolveInteraction?.(interaction.id, result);
  };

  return (
    <box style={{ flexDirection: "column", width: "100%", height: "100%", backgroundColor: theme.background }}>
      <Header cwd={state.snapshot.cwd} theme={theme} icons={icons} compact={compact} onHistory={() => void invoke("open_sessions")} onNew={() => void invoke("new_session")} />
      {panel ? <PanelView panel={panel} theme={theme} icons={icons} onClose={() => setPanel(null)} onSelect={selectPanel} /> : <Transcript items={state.items} theme={theme} icons={icons} />}
      {state.interaction ? <InteractionView interaction={state.interaction} theme={theme} icons={icons} onResolve={resolveInteraction} /> : panel ? null : <Composer key={composerVersion} value={draft} onChange={setDraft} onSubmit={submit} onCancel={onCancel ?? (() => undefined)} onExit={onExit} running={running} commands={state.commands} theme={theme} icons={icons} compact={compact} terminalWidth={width} disabled={false} onAction={onAction} />}
      <Footer theme={theme} snapshot={state.snapshot} compact={compact} onPermission={() => void invoke("toggle_permission")} />
    </box>
  );
}
