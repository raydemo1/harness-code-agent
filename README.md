# Harness Code Agent

Harness Code Agent 是一个基于 OpenAI-compatible Chat Completions API 的本地 autonomous coding agent 框架。它把“主 agent 负责完整执行闭环、只读子 agent 负责咨询”的模式封装成可复用的 profile，并提供工作区隔离、权限策略、会话记录、工具调用、浏览器测试、规划文件和 benchmark 适配能力。

项目适合用于：

- 在本地代码仓库中自动完成修复、重构、测试和验证任务。
- 从一句话 prompt 生成并迭代 Web 应用。
- 运行 Terminal-Bench / SWE-Bench 风格的任务。
- 构建和调试新的 agent profile、middleware、tool runtime 或 benchmark adapter。

## 功能特点

- **Profile 驱动**：内置 `coding-agent`、`app-builder`、`terminal`、`swe-bench`、`reasoning` 五种任务模式。
- **单主控 agent 架构**：主 agent 负责读代码、规划、修改、验证和最终决策；子 agent 仅用于只读调查、并行搜索、测试设计或 review。
- **OpenAI-compatible API**：通过 `OPENAI_BASE_URL` 和 `HARNESS_MODEL` 可切换到兼容 OpenAI 协议的服务。
- **工作区隔离**：默认在 `workspace/` 下创建独立任务目录，文件读写会经过路径检查。
- **运行时权限策略**：支持 `read-only`、`workspace-write`、`danger-full-access` 三种权限模式。
- **会话与事件记录**：每次运行会写入 `.harness/` 元数据、事件和文件快照。
- **工具系统**：支持文件读写、持久 shell、Web 搜索/抓取、规划文件、只读子 agent、可选浏览器测试等工具。
- **中间件防护**：包含循环检测、任务跟踪、错误恢复、时间预算、退出前验证等行为约束。
- **Benchmark 适配**：`benchmarks/` 下提供 Terminal-Bench 2.0 / Harbor 运行入口。

## 项目结构

```text
.
├── harness.py              # CLI 入口，转发到 harness_code_agent.cli
├── harness_code_agent/     # 核心 Python 包
│   ├── cli.py              # 命令行入口胶水
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

2. 安装 Python 依赖：

```bash
pip install -r requirements.txt
```

3. 如需浏览器测试，安装 Playwright 浏览器：

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
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o
HARNESS_WORKSPACE=./workspace
HARNESS_COMMIT_POLICY=checkpoint
```

## 快速开始

运行环境诊断：

```bash
python harness.py doctor
```

查看当前配置：

```bash
python harness.py config show
```

查看可用 profile：

```bash
python harness.py --list-profiles
```

执行默认本地代码任务。`run` 命令默认使用 `coding-agent` profile：

```bash
python harness.py run "Fix the failing tests"
```

指定 profile：

```bash
python harness.py run --profile terminal "Fix the broken symlinks in /tmp"
python harness.py run --profile swe-bench "Fix the TypeError in parse_config()"
python harness.py run --profile reasoning "Calculate the escape velocity of Mars"
```

兼容旧用法：不带 `run` 时默认使用 `app-builder` profile：

```bash
python harness.py "Build a simple kanban app in the browser"
```

## Profile 说明

| Profile | 用途 |
| --- | --- |
| `coding-agent` | 默认产品模式，用于本地仓库代码任务，带会话、权限、规划和验证 |
| `app-builder` | 从 prompt 构建完整 Web 应用，可使用浏览器测试工具 |
| `terminal` | 面向 Terminal-Bench 2.0 风格的 CLI / shell 任务 |
| `swe-bench` | 面向真实仓库 issue 修复任务 |
| `reasoning` | 面向知识密集型推理、计算或问答任务 |

## 会话和工作区

默认情况下，每次运行会在 `HARNESS_WORKSPACE` 下创建一个带时间戳的任务目录，例如：

```text
workspace/20260518-193000_fix-the-failing-tests/
```

该目录会自动初始化 Git 仓库，并在 `.harness/` 中记录 session metadata、events 和文件快照。任务结束后会根据 `HARNESS_COMMIT_POLICY` 决定是否写入 checkpoint commit。

常用会话命令：

```bash
python harness.py sessions
python harness.py session <session-id>
```

如果 benchmark 或外部 runner 需要直接在工作区根目录执行，可设置：

```bash
HARNESS_FLAT_WORKSPACE=1
```

## 配置项

核心配置来自 `.env` 或环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 空 | API key，必填 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API 地址 |
| `HARNESS_MODEL` | `gpt-4o` | 使用的模型名 |
| `HARNESS_WORKSPACE` | `./workspace` | agent 输出和会话目录 |
| `HARNESS_PERMISSION_MODE` | `workspace-write` | 权限模式：`read-only` / `workspace-write` / `danger-full-access` |
| `HARNESS_COMMIT_POLICY` | `checkpoint` | Git checkpoint 策略：`none` / `checkpoint` / `milestone` |
| `MAX_AGENT_ITERATIONS` | `60` | 单次 agent loop 最大迭代数 |
| `COMPRESS_THRESHOLD` | `80000` | 上下文压缩阈值 |
| `RESET_THRESHOLD` | `150000` | 上下文重置阈值 |

Profile 参数也可通过环境变量覆盖，格式为：

```bash
PROFILE_<PROFILE_NAME>_<KEY>=value
```

例如：

```bash
PROFILE_TERMINAL_TASK_BUDGET=1800
PROFILE_TERMINAL_PASS_THRESHOLD=8.0
```

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `read-only` | 允许读文件、搜索、只读子 agent；写入和 shell 命令需要批准 |
| `workspace-write` | 默认模式，允许工作区内读写和常规命令，高风险 shell 命令需要批准 |
| `danger-full-access` | 放行所有工具调用，适合受控 benchmark 环境 |

文件写入会通过 `WorkspaceService` 做路径约束，防止写出工作区；默认也会拒绝写入 `.git/` 和敏感 `.env` 文件。

## 测试

项目测试使用标准库 `unittest`：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

可单独运行某个测试文件：

```bash
python -m unittest tests.test_profiles
python -m unittest tests.test_main_agent_flow
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
3. 在 `execute_tool()` 路由中接入实现。
4. 根据风险更新 `harness_code_agent/runtime/permissions.py` 的工具分类。
5. 添加单元测试覆盖正常路径和失败路径。

新增 middleware 时，建议先明确它要拦截的是 tool call、tool result 还是 agent loop 退出条件，并在 `harness_code_agent/runtime/middlewares.py` 中保持行为可测试、可组合。

## 注意事项

- `.env` 不应提交到版本库，使用 `.env.template` 作为配置模板。
- `HARNESS_MODEL` 必须是目标 provider 可识别的模型名。
- `app-builder` 的浏览器能力依赖 Playwright；未安装时相关工具会不可用或报错。
- `terminal` profile 针对非交互式 CLI 任务优化，会更积极地执行 shell 命令和本地验证。
- 默认 `workspace-write` 模式仍会对高风险命令触发批准流程；自动化 benchmark 可按需切换权限模式。
