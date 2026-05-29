# Textual TUI 重构计划

## Summary
- 采用“增量双栈”迁移：先抽离 UI-agnostic 核心，再接入 Textual 全屏工作台，最后移除 prompt_toolkit 入口和依赖。
- 保持 CLI 公共入口不变：`TuiApp(cwd, profile_name, resume_session_id=None, first_task="").run() -> int`。
- 新 TUI 目标形态：全屏 Textual 工作台，包含 transcript、状态栏、输入区、审批/问题 modal、快捷键和实时 streaming。

## Key Changes
- 增加 `textual>=8.2,<9` 依赖；Textual 当前最新版为 8.2.7，官方支持 RichLog、TextArea、ModalScreen、Workers 和 `run_test()` 测试能力。
- 把现有 `TuiState` 改成纯数据状态，不再保存 prompt_toolkit fragments；渲染层改为 Rich/Textual renderables。
- 新增 TUI controller/presenter 层，统一处理 session submit、event queue、stream delta、slash command、cancel、permission toggle、manual compact。
- 用 Textual 实现全屏布局：`RichLog` transcript、`TextArea` composer、底部 context/permission/status bar、审批和问题 `ModalScreen`。
- 保留 approval allowlist、slash commands、mention candidates、thought toggle、Ctrl-C cancellation、first task auto-submit、non-TTY print mode行为。

## Implementation Steps
- 先写/改测试，覆盖纯状态模型、事件到 transcript block、completion 数据源、approval/question 结果、controller 队列和取消行为。
- 引入 Textual app skeleton，并用 Textual worker/thread worker 承载 blocking `InteractiveSession.submit()`；worker 内只通过 thread-safe message/callback 回主 UI。
- 将 approval/question provider 改为通过 Textual modal 同步等待结果，对外仍实现现有 `ApprovalProvider` / `QuestionProvider` 协议。
- 切换 `harness_code_agent.tui.__init__.TuiApp` 到 Textual 实现；旧 prompt_toolkit 实现仅在迁移期保留为 private fallback，完成后删除。
- 更新 README 中 “inline TUI” 描述为 “full-screen Textual TUI”，并移除 prompt_toolkit 依赖引用。

## Test Plan
- 单元测试：`python -m unittest tests.test_tui tests.test_compaction tests.test_interactive_cli`。
- 全量回归：`python -m unittest discover -s tests -p "test_*.py"`。
- Textual headless 测试：使用官方 `App.run_test()`/Pilot 测试输入提交、快捷键、modal 选择、状态栏点击和窗口尺寸变化。
- 手动验收：`hca`、`hca "task"`、`hca -p "task"`、`/help`、`/plan`、approval、question、Ctrl-C cancel、context compact。

## Assumptions
- 不新增除 Textual 以外的运行时依赖；不引入 `textual-dev` 到生产依赖。
- 全屏工作台是第一版目标，不保留 inline TUI 作为正式模式。
- 当前未提交改动不属于本次计划，实施时不回滚用户改动。
- 参考资料：[Textual PyPI](https://pypi.org/project/textual/)、[RichLog](https://textual.textualize.io/widgets/rich_log/)、[TextArea](https://textual.textualize.io/widgets/text_area/)、[Screens/ModalScreen](https://textual.textualize.io/guide/screens/)、[Workers](https://textual.textualize.io/guide/workers/)、[Testing](https://textual.textualize.io/guide/testing/)。
