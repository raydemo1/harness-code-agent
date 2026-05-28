# Harness Code Agent

Harness Code Agent 是一个基于 OpenAI-compatible Chat Completions API 的本地 autonomous coding agent 框架。它把“主 agent 负责完整执行闭环、只读子 agent 负责咨询”的模式封装成可复用的 profile，并提供本地仓库分析、权限策略、会话记录、工具调用、浏览器测试、规划文件和 benchmark 适配能力。

项目适合用于：

- 在本地代码仓库中自动完成修复、重构、测试和验证任务。
- 从一句话 prompt 生成并迭代 Web 应用。
- 运行 Terminal-Bench / SWE-Bench 风格的任务。
- 构建和调试新的 agent profile、middleware、tool runtime 或 benchmark adapter。

## 功能特点

- **Profile 驱动**：内置 `coding-agent`、`app-builder`、`terminal`、`swe-bench`、`plan` 五种任务模式。
- **单主控 agent 架构**：主 agent 负责读代码、规划、修改、验证和最终决策；子 agent 仅用于只读调查、并行搜索、测试设计或 review。
- **OpenAI-compatible API**：通过 `OPENAI_BASE_URL` 和 `HARNESS_MODEL` 可切换到兼容 OpenAI 协议的服务。
- **本地仓库工作流**：交互式模式默认使用启动 `hca` 时所在的当前目录，文件读写会经过路径检查。
- **运行时权限策略**：支持 `workspace-write`、`danger-full-access` 两种权限模式；受限调查/计划工作使用 `plan` profile。
- **会话与事件记录**：每次运行会写入 `.harness/` 元数据、事件和文件快照。
- **工具系统**：支持文件读写、持久 shell、用户选择提问、Web 搜索/抓取、规划文件、只读子 agent、可选浏览器测试等工具。
- **中间件防护**：包含循环检测、任务跟踪、错误恢复、时间预算、退出前验证等行为约束。
- **Benchmark 适配**：`benchmarks/` 下提供 Terminal-Bench 2.0 / Harbor 运行入口。
- **项目规则文件**：交互式 session 启动时会读取当前工作区根目录的 `HARNESS.md`，并作为稳定系统提示的一部分注入。
- **缓存友好上下文**：稳定规则放在前缀；工具结果和事实失效提示采用追加式历史，避免每轮改动前部上下文，提高 prompt cache 命中率。warmup 后平均缓存命中率从约 `49.8%` 提升到约 `87.5%`，第 6 轮从 `4352/8187 = 53.2%` 提升到 `7296/8148 = 89.5%`。

## 项目结构

```text
.
├── harness_code_agent/     # 核心 Python 包
│   ├── cli.py              # `hca` 命令行入口
│   ├── core/               # Harness 控制器、CLI 命令处理、日志配置
│   ├── agent/              # Agent loop、运行状态、上下文压缩/恢复
│   ├── runtime/            # 工具、权限、审批、middleware 和 tool context
│   ├── workspace/          # 工作区路径保护、快照和持久 shell
│   ├── sessions/           # 会话元数据与事件日志
│   ├── skills/             # skill registry
│   └── profiles/           # 不同任务场景的 profile
├── skills/                 # agent 可读取的本地技能说明
├── benchmarks/             # benchmark launcher 和 Harbor adapter
└── tests/                  # unittest 测试
```

## 环境要求

- Python 3.10+
- Git
- 可用的 OpenAI-compatible API key
- 可选：Playwright Chromium 浏览器，用于 `app-builder` 相关浏览器测试
- 可选：Docker 和 Harbor，用于 Terminal-Bench 任务

## 安装

1. 克隆或进入项目目录：

```bash
cd harness-code-agent
```

2. 以 editable 方式安装项目和依赖，这会注册 `hca` 命令：

```bash
pip install -e .
```

如果只想在源码目录内临时运行，也可以安装 `requirements.txt` 后使用 `python -m harness_code_agent.cli`，但不会自动注册 `hca` console script。

3. 如需 `app-builder` 浏览器测试，安装 Playwright Chromium：

```bash
python -m playwright install chromium
```

4. 创建并填写 `.env`：

```bash
cp .env.template .env
```

Windows PowerShell 可以使用：

```powershell
Copy-Item .env.template .env
```

然后编辑 `.env`：

```env
OPENAI_API_KEY=sk-your-deepseek-key-here
OPENAI_BASE_URL=https://api.deepseek.com
HARNESS_MODEL=deepseek-v4-flash
```

## 快速开始

运行环境诊断：

```bash
hca
# 然后在交互提示符中输入：
/doctor
```

查看当前配置：

```bash
hca
/config show
```

查看可用 profile：

```bash
hca --list-profiles
```

进入交互式本地开发助手。默认 profile 是 `coding-agent`，工作区就是启动 `hca` 时所在的当前目录；TTY 中会打开 Claude Code 风格的 inline TUI：

```bash
hca
```

然后在 TUI 输入栏中提交任务，例如 `Fix the failing tests`。

也可以启动后立即提交第一个任务，然后继续留在交互模式：

```bash
hca "Fix the failing tests"
```

如果需要给 E2E 或脚本使用，可以提交第一个任务后直接退出：

```bash
hca -p "Fix the failing tests"
hca --print "Fix the failing tests"

# 也支持管道输入
echo "Fix the failing tests" | hca
```

指定 profile：

```bash
hca --profile terminal "Fix the broken symlinks in /tmp"
hca --profile swe-bench "Fix the TypeError in parse_config()"
hca --profile plan "Design the fix for the failing parser tests"
```

恢复旧 session 作为上下文：

```bash
hca --resume <session-id>
hca --resume <session-id> "Continue the previous parser work"
```

交互模式里可以用短 slash 命令切换 profile：

```text
/code      # coding-agent，默认实施模式
/plan      # 受限方案模式（只允许计划/状态工件写入）
/terminal  # CLI / shell 任务
/swe       # swe-bench issue 修复
/app       # app-builder Web 应用构建
```

## Profile 说明

| Profile | 用途 |
| --- | --- |
| `coding-agent` | 默认产品模式，用于本地仓库代码任务，带会话、权限、规划和验证 |
| `app-builder` | 从 prompt 构建完整 Web 应用，可使用浏览器测试工具 |
| `terminal` | 面向 Terminal-Bench 2.0 风格的 CLI / shell 任务 |
| `swe-bench` | 面向真实仓库 issue 修复任务 |
| `plan` | 调查和方案设计，输出结构化 Markdown 计划；只允许写计划 Markdown 和 planning state |

`plan` profile 在交互模式下会进入显式 handoff：计划输出后不会自动改代码，而是提示用户选择下一步。

```bash
hca
/plan
设计 parser 修复方案
# 查看计划后：
继续
# 或继续修改计划：
补充兼容性风险和回滚方案
```

用户回复“继续”“执行”“开始”等短确认时，CLI 会切换到默认 `coding-agent` profile，并把刚才的 Markdown 计划注入为 approved plan；如果回复的是一段修改建议，则继续停留在 `plan` profile 修改计划。

## 会话和工作区

交互模式默认直接在当前目录工作，不再为开发任务创建带时间戳的工作区。该目录会自动初始化 Git 仓库，并在 `.harness/` 中记录 session metadata、events 和文件快照。

自动 checkpoint 默认在每个完成的 turn 后运行，可在交互模式中用 `/checkpoint auto off` 关闭，或用 `/checkpoint every <N> turns` 调整频率。自动 checkpoint 只会尝试提交本轮新增的可提交变更；如果没有本轮新增变更，会提示没有需要 checkpoint 的内容。本轮开始前已经存在的 dirty 文件不会被自动提交；如果本轮开始前已有 staged changes，自动 checkpoint 会跳过，避免混入用户已暂存内容。

常用会话命令：

```bash
hca
/help
/profiles
/sessions
/session <session-id>
/resume <session-id>
/fork <session-id>
/rollback <session-id> <path>
/checkpoint status
/compact show
```

非交互入口也保留了一个快速查看最新 session 的兼容命令：

```bash
hca session show latest
```

可以用 `@` mention 把文件或历史 session 作为上下文注入当前 turn：

```text
根据 @README.md 修复文档里的启动示例
继续 @session:20260518-120000-abcd1234 里的工作
根据 @"docs/path with spaces.md" 补充测试说明
```

## 配置项

核心配置来自 `.env` 或环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 空 | API key，必填 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API 地址 |
| `HARNESS_MODEL` | `gpt-4o` | 使用的模型名 |
| `HARNESS_PROVIDER` | `auto` | Provider adapter：`auto` / `openai` / `deepseek` / `openai-compatible` |
| `HARNESS_STREAM` | `auto` | CLI streaming：`auto` 表示仅 TTY 实时输出，`1` 强制开启，`0` 关闭 |
| `HARNESS_WINDOWS_SHELL` | `auto` | Windows shell 后端：`auto` / `pwsh` / `powershell` / `cmd` |
| `HARNESS_PERMISSION_MODE` | `workspace-write` | 权限模式：`workspace-write` / `danger-full-access` |
| `HARNESS_CONTEXT_WINDOW_TOKENS` | `128000` | 上下文窗口估算值，用于推导默认压缩/重置阈值 |
| `COMPRESS_THRESHOLD` | `96000` | 上下文压缩阈值；默认是 `HARNESS_CONTEXT_WINDOW_TOKENS * 0.75` |
| `RESET_THRESHOLD` | `104960` | 上下文重置阈值；默认是 `HARNESS_CONTEXT_WINDOW_TOKENS * 0.82` |
| `MAX_AGENT_ITERATIONS` | `60` | 单次 agent loop 最大迭代数 |
| `HARNESS_TRACE_STDERR` | 空 | 设置为 `1` / `true` / `yes` / `on` 时输出底层 API 错误追踪 |
| `MAX_HARNESS_ROUNDS` | `5` | 兼容保留的 harness loop / benchmark 调参项 |
| `PASS_THRESHOLD` | `7.0` | 兼容保留的 harness loop / benchmark 调参项 |

交互式模式会把启动 `hca` 时所在的目录作为当前工作目录，不需要在 `.env` 中配置 `HARNESS_WORKSPACE`。代码中的 `config.WORKSPACE` 仍作为运行时内部字段使用，用来告诉工具、会话记录和权限检查“当前项目根目录”在哪里。

Profile 参数也可通过环境变量覆盖，格式为：

```bash
PROFILE_<PROFILE_NAME>_<KEY>=value
```

例如：

```bash
PROFILE_TERMINAL_TASK_BUDGET=1800
PROFILE_TERMINAL_TIME_WARN_THRESHOLD=0.45
```

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `workspace-write` | 默认模式，允许读工具、路径受控的项目内结构化写工具、白名单 shell；非白名单 shell 和未知工具需要批准；极危黑名单命令永远阻断 |
| `danger-full-access` | 放行非黑名单工具调用，适合受控 benchmark 环境；极危黑名单命令仍永远阻断 |

文件写入会通过 `WorkspaceService` 做路径约束，防止写出工作区；默认也会拒绝写入 `.git/` 和敏感 `.env` 文件。
受限调查/计划工作请使用 `--profile plan`；该 profile 只暴露必要读工具、只读 shell、用户提问/子 agent 咨询和 `update_plan_state`。

## 测试

项目测试使用标准库 `unittest`：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

可单独运行某个测试文件：

```bash
python -m unittest tests.test_profiles
python -m unittest tests.test_product_runtime
```

## Terminal-Bench 运行

`benchmarks/` 目录提供 Harbor adapter。基础准备：

```bash
pip install harbor
docker info
```

确保 `.env` 中 API 配置可用后，运行单个任务：

```bash
python benchmarks/run_terminal_bench.py --task fix-git
```

运行多个任务：

```bash
python benchmarks/run_terminal_bench.py --task fix-git --task query-optimize
```

运行本地完整数据集：

```bash
python benchmarks/run_terminal_bench.py --full
```

使用 Daytona 云环境：

```bash
python benchmarks/run_terminal_bench.py --task fix-git --env daytona
```

更多细节见 `benchmarks/README.md`。

## 开发指南

新增 profile 时，通常需要：

1. 在 `harness_code_agent/profiles/` 下新增继承 `BaseProfile` 的类。
2. 实现 `name()`、`description()`、`main_agent()`，必要时覆盖 `acceptance_criteria()`。
3. 在 `harness_code_agent/profiles/__init__.py` 的 `PROFILES` 中注册。
4. 为 profile 的关键行为添加测试。

新增工具时，通常需要：

1. 在 `harness_code_agent/runtime/tools.py` 中实现工具函数。
2. 在 tool schema 中声明参数。
3. 在 `_build_builtin_tool_registry()` 中注册 handler，并显式声明权限分类。
   例如查询天气这类外部只读工具应使用 `TOOL_PERMISSION_NETWORK_READ`。
4. 如果现有分类不够表达风险，再扩展 `harness_code_agent/runtime/permissions.py` 的权限分类。
5. 添加单元测试覆盖正常路径和失败路径。

新增 middleware 时，建议先明确它要拦截的是 tool call、tool result 还是 agent loop 退出条件，并在 `harness_code_agent/runtime/middlewares.py` 中保持行为可测试、可组合。

## 注意事项

- `.env` 不应提交到版本库，使用 `.env.template` 作为配置模板。
- `HARNESS_MODEL` 必须是目标 provider 可识别的模型名。
- `app-builder` 的浏览器能力依赖 Playwright；未安装时相关工具会不可用或报错。
- `terminal` profile 针对非交互式 CLI 任务优化，会更积极地执行 shell 命令和本地验证。
- 默认 `workspace-write` 模式会对白名单外 shell 命令和未知工具触发批准流程；自动化 benchmark 可按需切换权限模式。
