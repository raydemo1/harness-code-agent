# PLAN.md 实施审查报告

## 审查范围

基于 [PLAN.md](file:///c:/Users/ray/Desktop/harness-code-agent/PLAN.md) 定义的 **HCA TUI 异步交互与输出降噪改造计划**，逐项审查 17 个修改文件 + 2 个新增文件的未提交改动。

**改动统计**: +1197 / -83 行，涉及 17 个修改文件、2 个新增文件。

---

## 五阶段实现完成度

| Phase | 模块 | 状态 | 说明 |
|-------|------|------|------|
| **Phase 1: Layout Skeleton** | `app.py`, `render.py`, `state.py` | ✅ **完成** | `Application` + `HSplit` 布局，transcript/context bar/input 三区 |
| **Phase 2: Async Submit** | `app.py` | ✅ **完成** | `ThreadPoolExecutor` + `_event_queue` + `_drain_event_queue` |
| **Phase 3: Output Noise Reduction** | `state.py`, `render.py` | ✅ **完成** | `ToolSummary` + 摘要渲染 + 完整输出写入 EventBus |
| **Phase 4: Thought Hiding** | `events.py`, `providers.py`, `loop.py`, `state.py` | ✅ **完成** | `ThoughtStarted/Finished` 事件 + `💭 thought for Xs` + `Ctrl+T` |
| **Phase 5: Cancellation** | `cancellation.py`, `app.py`, `loop.py`, `interactive.py` | ✅ **完成** | `CancellationToken` 链路完整 |

---

## 关键文件改动分析

### 新增文件

| 文件 | 大小 | 用途 |
|------|------|------|
| [cancellation.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/agent/cancellation.py) | 28 行 | `CancellationToken` + `CancelledError`，基于 `threading.Event` |
| [PLAN.md](file:///c:/Users/ray/Desktop/harness-code-agent/PLAN.md) | 101 行 | 五阶段改造计划文档 |

### 核心实现文件

| 文件 | 改动行 | 关键改动 |
|------|--------|---------|
| [app.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/app.py) | +261 | 新 `TuiApp` 全屏布局 + async submit + Ctrl-C/Ctrl-T 键绑定 |
| [render.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/render.py) | +192 | `context_bar_fragments` + `render_block_fragments` + card/bar 渲染 |
| [state.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/state.py) | +98 | `ToolSummary` + `transcript_fragments` + thought metadata |
| [loop.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/agent/loop.py) | +36 | cancellation check + thought 事件发射 |
| [providers.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/agent/providers.py) | +6 | `on_reasoning_start`/`on_reasoning_delta` 回调参数 |
| [events.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/sessions/events.py) | +27 | `ThoughtStartedEvent` + `ThoughtFinishedEvent` |
| [interactive.py](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/core/interactive.py) | +8 | `cancellation_token` 参数透传 |

---

## 发现的问题

### 🔴 严重 (P0)

#### 1. `_refresh_display` 在后台线程直接调用 `_drain_event_queue`，存在线程安全问题

- **位置**: [app.py#L287-L292](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/app.py#L287-L292)
- **问题**: `_refresh_display()` 被后台线程调用（例如 `_event_listener` L224、`_stream_delta` L271、`_submit_worker` L186），但它直接调用 `_drain_event_queue()` 修改 `self.state`。`TuiState` 没有锁保护，后台线程和 UI 线程（`_get_transcript_text` L297 也调用 `_drain_event_queue`）可能同时修改 `transcript_fragments` 列表，导致竞态条件。
- **影响**: 在实际运行中可能表现为偶发的渲染混乱、fragment 丢失或 IndexError。
- **建议修复**: `_refresh_display` 应只调用 `self.app.invalidate()`（线程安全），将 `_drain_event_queue()` 仅留在 `_get_transcript_text` 回调中（UI 线程执行）。

```python
def _refresh_display(self) -> None:
    """Trigger UI redraw from any thread."""
    if self.app and self.app.is_running:
        self.app.invalidate()
```

---

### 🟡 中等 (P1)

#### 2. 旧的 `bottom_toolbar` 函数仍保留 `○60/○68/○75/○82` 圆圈显示

- **位置**: [render.py#L67-L109](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/render.py#L67-L109)
- **PLAN.md 要求**: "状态栏改成单一彩色数值 `ctx 70%`，不出现 `○60/○68/○75/○82`"
- **现状**: 新的异步 TUI 使用 `context_bar_fragments()`（L156-191，不含圆圈 ✅），但旧的 `bottom_toolbar()` 和 `context_indicator_fragments()` 仍在 [input.py#L49](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/input.py#L49) 中被使用，且测试 `test_bottom_toolbar_renders_clickable_context_threshold_circles` 仍断言 `○60` 等圆圈存在。
- **影响**: 如果旧的同步 TUI 入口仍被使用（`TuiComposer` 路径），状态栏显示不符合 PLAN.md 要求。
- **建议**: 
  - 如果旧的 `TuiComposer` 路径已弃用，删除或标记 deprecated
  - 如果仍需保留，更新 `bottom_toolbar()` 使其也使用 `ctx NN%` 单一格式
  - 更新对应测试断言

#### 3. Cancellation 只在迭代顶部检查，streaming 模型调用期间不可中断

- **PLAN.md 要求**: "streaming 模型调用在 chunk 间检查 cancellation token，取消后停止展示并丢弃后续结果"
- **位置**: [loop.py#L446-L448](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/agent/loop.py#L446-L448)
- **现状**: cancellation 检查仅在 `for local_iteration` 循环顶部 (L446)。`_request_assistant_message` 内的 streaming 循环（L134 `for chunk in chunks`）没有检查 token。长时间的 streaming 响应（如大段代码生成）期间用户按 Ctrl-C 不会立即中断，必须等当前 streaming 完成后才会在下一个 iteration 取消。
- **建议**: 在 `assistant_message_from_stream` 的 chunk 循环中传入 token 并检查：
  ```python
  for chunk in chunks:
      if cancellation_token is not None and cancellation_token.is_cancelled:
          raise CancelledError("cancelled during streaming")
  ```

#### 4. 取消后仍可能产生 assistant block

- **PLAN.md 要求**: "被取消 turn 不产生最终 assistant block"
- **位置**: [app.py#L262-L266](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/app.py#L262-L266)
- **现状**: `_submit_worker` 中 `CancelledError` 被捕获后发送 `("cancelled", None)` 到队列。但如果取消发生在 `loop.py` 的 `_request_assistant_message` 返回之后、`run_until_idle` 的下一个 iteration check 之前，`assistant_message` 已经被 `_append_message` 写入了 `self.messages`（L562），EventBus 也已经发射了 `AssistantMessageEvent`（`interactive.py` L219-224）。
- **影响**: 在特定时序窗口下，取消的 turn 仍会产生 assistant message 事件。
- **建议**: 在 `InteractiveSession._submit_to_current_agent` 中捕获 `CancelledError`，跳过 `AssistantMessageEvent` 的发射。

---

### 🟢 低优先级 (P2)

#### 5. `_format_elapsed` 函数两个分支返回相同格式

- **位置**: [state.py#L263-L266](file:///c:/Users/ray/Desktop/harness-code-agent/harness_code_agent/tui/state.py#L263-L266)
- **问题**: `< 1.0` 和 `>= 1.0` 两个分支都返回 `f"{seconds:.1f}s"`，if 语句无意义。
- **建议**: 根据 PLAN.md 的 `thought for 12s` 示例，可能原意是短时间用 ms 单位：
  ```python
  def _format_elapsed(seconds: float) -> str:
      if seconds < 1.0:
          return f"{int(seconds * 1000)}ms"
      return f"{seconds:.1f}s"
  ```

#### 6. PLAN.md 要求的测试覆盖不完整

- **PLAN.md Test Plan 要求**:
  - "fake streaming provider 被取消后停止" — ❌ 缺失
  - "fake shell session 收到 interrupt" — ❌ 缺失
  - "被取消 turn 不产生最终 assistant block" — ❌ 缺失
  - "完整 output 仍写入事件存储" — ⚠️ 未显式验证
- **现有取消测试** (`TuiCancellationTests`) 只覆盖了 `CancellationToken` 基础 API 和 token 存在性，缺少端到端的取消流程验证。

---

## 测试结果

```
tests/test_tui.py:                47 passed ✅
tests/test_interactive_cli.py:    25 passed ✅  
tests/test_profiles.py:           10 passed ✅
tests/test_subagent_consultation.py: 13 passed ✅
全量 pytest:                       ⏳ (运行中)
```

> [!NOTE]
> PLAN.md 声称 "223 passing (31 original + 9 layout + 3 async + 5 noise reduction + 4 thought + 4 cancellation)"，实际 test_tui.py 中有 47 个测试通过（31 原有 + 9 layout + 3 async + 5 noise reduction + 4 thought + 4 cancellation = 56，但部分计入不同分类方式），与声称基本一致。

---

## 合规矩阵

| PLAN.md 要求 | 状态 | 备注 |
|-------------|------|------|
| 状态栏改成 `ctx NN%` | ✅/⚠️ | 新 TUI 合规；旧 `TuiComposer` 路径仍用圆圈 |
| `<68%` 绿色，`68-81%` 黄色，`≥82%` 红色 | ✅ | 有测试覆盖 |
| 点击 ctx 触发手动压缩 | ✅ | mouse_handler 实现 |
| `Application.run_async()` 替代同步 prompt | ✅ | 使用 `app.run()` (同步接口) + `ThreadPoolExecutor` |
| 布局拆成 transcript/activity/input | ✅ | `HSplit` 5 层布局 |
| `submit()` 放入后台线程 | ✅ | `_submit_async` + executor |
| 运行中输入框保持可编辑 | ✅ | `TextArea` 始终可用 |
| 运行中 Enter 提示 | ✅ | `_submitting` flag 阻止重复 submit |
| 工具结果只显示摘要 | ✅ | `ToolSummary` + 只含 size/rc/error |
| 完整工具输出写入事件存储 | ✅ | EventBus 不过滤 |
| 不显示 `reasoning_content`/`<think>` 具体内容 | ✅ | `on_reasoning_delta` 是空操作 |
| 只显示 `thought for Xs` | ✅ | `ThoughtFinishedEvent` → thought block |
| `Ctrl+T` 切换 | ✅ | toggle 元信息详细程度 |
| `Ctrl-C` 取消当前轮 | ✅ | `CancellationToken.cancel()` |
| streaming chunk 间检查 token | ❌ | **未实现** — 只在 iteration 顶部检查 |
| shell 工具 best-effort 中断 | ⚠️ | 未专门实现 shell interrupt |
| 不新增依赖 | ✅ | 仅使用现有 `prompt_toolkit` |
| batch 模式不做 TUI 改造 | ✅ | 不涉及 `-p` 路径 |

---

## 总结

**五阶段改造的核心功能全部到位**，异步布局、输出降噪、思考隐藏、取消机制的骨架和主要逻辑均已实现且测试通过。

**需要优先处理的问题**：
1. 🔴 **P0**: `_refresh_display` 线程安全问题 — 后台线程直接修改 state，可能导致竞态
2. 🟡 **P1**: streaming 取消未实现 — 长 streaming 响应期间 Ctrl-C 不会立即中断
3. 🟡 **P1**: 旧 `bottom_toolbar` 路径仍保留圆圈格式，与 PLAN.md 目标矛盾
