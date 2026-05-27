# HCA TUI 异步交互与输出降噪改造计划

## Summary

当前 TUI 是同步轮次模型：输入后 `session.submit()` 阻塞执行，事件直接打印到 stdout，导致输入框不常驻、工具输出刷屏、运行中难取消。

目标改成 Claude Code CLI 风格的常驻 TUI：输入栏一直可见，agent 后台运行，工具/过程默认只显示摘要，`Ctrl-C` 取消当前轮。思考过程默认不展示，也不展示具体思考内容；只显示类似 `thought for 12s` 的元信息，并通过快捷键切换显示/隐藏。

## Key Changes

- 状态栏改成进度条 + 数值。
  - 显示形态：`ctx ▓▓▓▓▓▓▓▓▓▓░░░░░░░░ 70%  96K/128K`。
  - `<68%` 绿色（#a3be8c），`68%-81%` 黄色（#ebcb8b），`>=82%` 红色（#bf616a）。
  - 点击 `ctx NN%` 触发手动压缩；hover 弹出 tooltip 显示 `tokens/window` 和阈值详情。
  - 深色底栏（#2d2d3d）独立区域。

- 重构 TUI 为常驻异步界面。
  - 用 `prompt_toolkit.Application.run_async()` 替代同步 `PromptSession.prompt()`。
  - 布局拆成 transcript 区、activity/status 区、常驻 input 区、bottom toolbar。
  - `InteractiveSession.submit()` 放入后台线程执行，事件通过线程安全队列回到 UI 线程刷新。
  - 运行中输入框保持可编辑；按 Enter 时如果 agent 正在运行，提示“当前轮运行中，Ctrl-C 可取消”。

- 输出降噪。
  - 工具结果默认只显示摘要：工具名、状态、关键参数、退出码/错误摘要、输出长度。
  - 成功工具不展示正文；失败工具只展示短错误摘要。
  - 完整工具输出继续写入 session events/trace，不进入默认 TUI transcript。

- 思考过程展示策略。
  - 默认不显示 `reasoning_content`、`<think>...</think>` 或任何具体思考文本。
  - transcript 中只显示元信息，例如 `thought for 12s`。
  - 使用快捷键 `Ctrl+T` 切换 thought 元信息区域显示/隐藏。
  - 即使展开，也只显示耗时、阶段、是否被截断等元数据，不显示具体思考内容。

- 取消语义。
  - `Ctrl-C` 在运行中取消当前轮，保留会话和输入栏。
  - streaming 模型调用在 chunk 间检查 cancellation token，取消后停止展示并丢弃后续结果。
  - shell 工具调用现有 shell interrupt 做 best-effort 中断。
  - 不可立即中断的阻塞调用标记为 cancelled，后台返回后丢弃结果。

## Interfaces

- 新增内部取消模型：`CancellationToken`，传入 `InteractiveSession.submit()`、`AgentConversation.run_until_idle()`、provider stream parser。
- `TuiState` 增加 visible transcript、activity feed、thought metadata 三类状态。
- 新增 UI 工具摘要结构：`tool`、`status`、`args_summary`、`output_chars`、`return_code`、`error_summary`、`started_at`、`finished_at`。
- 新增 thought 元信息结构：`duration_seconds`、`status`、`source`、`truncated`；不包含具体 reasoning 文本。
- `TuiApprovalProvider` 改成异步 UI 协调：后台线程发起 approval request，UI 内联展示选择，用户选择后唤醒后台线程。

## Test Plan

- 状态栏：渲染 `ctx ▓▓▓▓▓▓▓▓▓▓░░░░░░░░ 70%  96K/128K`；颜色随百分比变化（绿/黄/红）；点击触发手动压缩。
- 输出降噪：大工具输出不进入 visible transcript，只显示摘要；完整 output 仍写入事件存储。
- 思考隐藏：`reasoning_content` 和 `<think>...</think>` 不显示具体内容；只生成 `thought for xx s` 元信息；`Ctrl+T` 只切换元信息区域。
- 异步交互：提交任务后 input 常驻；运行中 Enter 不启动第二个并发 turn；`Ctrl-C` 调用取消逻辑。
- 取消：fake streaming provider 被取消后停止；fake shell session 收到 interrupt；被取消 turn 不产生最终 assistant block。
- 回归：跑 `tests/test_tui.py`、`tests/test_interactive_cli.py`、`tests/test_compaction.py`，最后跑全量 `D:\miniconda\miniconda\python.exe -m pytest -q`。

## Assumptions

- 不新增依赖，继续使用现有 `prompt_toolkit>=3.0.0`。
- batch/`-p` 模式不做 TUI 改造。
- `/compact show` 继续保留。
- 取消不回滚已经发生的文件修改，只停止当前 agent 轮次继续推进。
- thought 快捷键默认使用 `Ctrl+T`，后续如有键位冲突再调整。

## Implementation Status

**Phase 1: Layout Skeleton** — Done

- `app.py`: Full `Application` + `HSplit` layout with transcript, context bar, input area
- `render.py`: `context_bar_fragments()`, `render_block_fragments()`, card styling for tool/thought
- `state.py`: `transcript_fragments`, `add_block_fragments()`, `append_streaming_text()`

**Phase 2: Async Submit** — Done

- `app.py`: `ThreadPoolExecutor`, `_submit_async()`, `_event_queue`, `_drain_event_queue()`
- Event flow: background thread → queue → UI thread drain → state update → invalidate
- Input stays editable during execution

**Phase 3: Output Noise Reduction** — Done

- `state.py`: `ToolSummary` dataclass, `_pending_tools` timing, summary-only tool result blocks
- `render.py`: Card rendering for tool blocks (`┌─ tool ─┐`)
- Full output stays in EventBus trace, not in visible transcript

**Phase 4: Thought Hiding** — Done

- `events.py`: `ThoughtStartedEvent`, `ThoughtFinishedEvent`
- `providers.py`: `on_reasoning_start`, `on_reasoning_delta` callbacks
- `loop.py`: Emits thought events when `reasoning_content` detected during streaming
- `state.py`: `show_thought_details`, `toggle_thought_details()`, thought block with `💭 thought for Xs`
- `app.py`: `Ctrl+T` toggles thought metadata details

**Phase 5: Cancellation** — Done

- `cancellation.py`: `CancellationToken`, `CancelledError`
- `app.py`: `Ctrl-C` cancels current turn, new token per submit
- `loop.py`: Checks token at each iteration, raises `CancelledError`
- `interactive.py`: Passes `cancellation_token` through `submit()` → `run_until_idle()`
- Cancelled turn shows "turn cancelled" in transcript, no assistant block

**Tests**: 223 passing (31 original + 9 layout + 3 async + 5 noise reduction + 4 thought + 4 cancellation)
