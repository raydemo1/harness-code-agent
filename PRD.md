# Session 可观测性与回放能力

## 目标与背景

当前项目需要把一次 agent session 中发生的关键动作沉淀成稳定、可读、可统计的数据。MVP 的重点不是做完整分析平台，而是先建立不会轻易返工的数据基础：类型化工具结果、结构化事件、人类可读总结，以及不用直接读 JSONL 的最新 session 查看入口。

这组能力会支撑后续评估、故障分析、回放、产品调试和自动化报告。

## 目标用户

- 使用本地 harness 运行 coding agent 的开发者。
- 需要排查 agent 行为、工具调用和失败原因的维护者。
- 后续需要对 session 进行统计、评估或回放的产品/工程人员。

## 核心场景

- 用户运行一次 agent 后，可以快速查看最新 session 的人类可读摘要。
- 维护者可以依赖稳定事件类型理解一次 session 的时间线，而不是猜测松散 JSON 字段。
- 工具调用结果可以通过 typed `ToolResult` 表达成功、失败、输出、错误和元数据，减少后续改动成本。
- 后续统计与回放可以基于 `FinalReportEvent`、失败分类和事件时间线继续扩展。

## MVP 范围

1. `typed ToolResult`
   - 建立明确的工具结果数据结构。
   - 至少表达工具名、成功状态、输出、错误信息、返回码或等价状态、元数据。
   - 现有工具执行路径应尽量通过该结构向 session 层传递结果。

2. `structured event schema`
   - 定义稳定事件模型，不允许只是随意 append JSON。
   - MVP 至少包含以下事件：
     - `UserInputEvent`
     - `AssistantMessageEvent`
     - `ToolCallEvent`
     - `ToolResultEvent`
     - `FileChangeEvent`
     - `FailureEvent`
     - `FinalReportEvent`

3. `session summary`
   - 每次 session 结束时自动生成一份人类可读 summary。
   - summary 应包含 session id、开始/结束时间、用户输入数量、助手消息数量、工具调用数量、失败数量、文件变更数量，以及最终状态或最后报告。

4. `session show latest`
   - 提供命令行入口，让用户可以查看最新 session 的摘要和关键事件。
   - 用户不需要直接打开或阅读 JSONL 文件。

## 修改内容

unknown tool 不会写事件

这一段有个小坑：

fn = BUILTIN_TOOL_REGISTRY.get(name)
if fn is None:
    return f"[error] Unknown tool: {name}"

它在 tool_context 事件记录之前就直接返回了。也就是说，如果模型调用了一个不存在的工具，模型会看到 [error] Unknown tool: ...，但 session JSONL 里可能不会有 tool_call、tool_result、failure。

这对可观测性是不完整的。

建议改成：即使 unknown tool，也构造 ToolResult(ok=False) 并 emit。

3 对重复函数：

裸函数 _with_context 包装差异
read_file (L46) _read_file_with_context (L1331) 路径解析、截断逻辑重复；包装多了 ToolResult 包装
write_file (L75) _write_file_with_context (L1359) 文件操作逻辑重复；包装多了事件发射，但有 bug（错误路径返回裸字符串）
apply_patch (L84) _apply_patch_with_context (L1393) 同上，同样的 bug
9 个无包装的工具：全部返回裸字符串 → ok=None，即使明确写了 [error]

dispatch 分支（L1255-1265）：一串 if/elif 手动选择走哪个版本，脆弱且扩展性差。

修复方案：统一到一个模式
核心思路：每个工具函数只写一次，接收可选的 tool_context，统一返回 ToolResult，副作用（事件发射）统一在 execute_tool 中处理.

把 ok: bool | None 改成显式 status: "success" | "failed" | "unknown"，然后所有判断都基于 status，不要基于 not ok 来判断失败，因为现在 unknown 也会是 not ok。

## 第二阶段范围

5. `failure classification`
   - 先做简单分类，例如 `tool_error`、`runtime_error`、`user_cancelled`、`validation_error`、`unknown`。
   - 不在第二阶段引入复杂根因分析或机器学习分类。

6. `final report event`
   - 在 MVP 的事件 schema 中先定义 `FinalReportEvent`。
   - 第二阶段增强其内容，使其可直接用于统计、评估和回放结束状态。


## 功能清单

- 类型化工具结果模型。
- 结构化 session event schema。
- JSONL 持久化兼容结构化事件。
- session 结束 summary 自动生成。
- 查看最新 session 的 CLI 命令。
- 第二阶段预留失败分类、最终报告增强和 replay/inspect。

## 页面 / 工作流地图

这是 CLI / 本地运行时能力，不包含图形页面。

核心工作流：

1. 用户运行 agent。
2. 用户输入被记录为 `UserInputEvent`。
3. 助手输出被记录为 `AssistantMessageEvent`。
4. 工具执行开始记录为 `ToolCallEvent`。
5. 工具执行结果记录为 `ToolResultEvent`，结果来自 typed `ToolResult`。
6. 文件变更或失败分别记录为 `FileChangeEvent`、`FailureEvent`。
7. session 结束时记录 `FinalReportEvent` 并生成 summary。
8. 用户运行 `session show latest` 类命令查看最新 session。

## 验收标准

- 仓库中存在中文 `PRD.md`，并清晰区分 MVP 与第二阶段。
- 代码中存在 typed `ToolResult`，且至少有测试覆盖成功和失败工具结果序列化或记录行为。
- 代码中存在结构化事件定义，事件类型至少覆盖 MVP 列出的七种事件。
- session 持久化时事件包含稳定 `type` 字段和必要时间戳/标识信息。
- session 结束时会生成人类可读 summary，且 summary 可被测试读取或断言。
- CLI 提供查看最新 session 的入口，并有测试证明用户无需读取 JSONL 即可看到摘要。
- 现有相关测试继续通过。

## 非目标

- MVP 不做完整 replay UI。
- MVP 不做复杂 failure taxonomy。
- MVP 不引入数据库或远程服务。
- MVP 不做跨机器 session 同步。
- MVP 不重构无关 agent loop 或工具执行架构。

## 技术方向

- 优先沿用现有 `harness_code_agent/sessions`、`runtime`、`cli` 结构。
- 使用 Python 标准库数据结构，例如 `dataclass`、`TypedDict` 或等价类型提示，保持实现可读、可测试。
- JSONL 仍可作为底层持久化格式，但写入内容必须来自结构化事件模型。
- CLI 命令应匹配现有命令风格，避免引入新的命令框架。

## 风险与依赖

- 如果事件 schema 改得太晚，历史 JSONL 兼容成本会升高。
- 如果 typed `ToolResult` 没有进入主要工具执行路径，后续统计仍会依赖不稳定字段。
- 当前仓库已有未提交改动，实现时必须避免回退无关文件。
- summary 自动生成应避免阻塞 session 正常结束；失败时应记录错误而不是破坏主流程。

## 第一阶段实现切片

第一阶段只实现 MVP 1-4：

1. 增加 typed `ToolResult`。
2. 增加结构化事件 schema。
3. 在 session 结束时生成人类可读 summary。
4. 增加查看最新 session 的 CLI 入口。

建议验证方式：

- 先写失败测试覆盖 typed `ToolResult`、事件 schema、summary 生成和 latest session CLI。
- 实现最小代码后运行聚焦测试。
- 聚焦测试通过后运行相关 session/CLI 测试。
