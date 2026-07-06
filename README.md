# Harness Code Agent

Harness Code Agent，简称 HCA，是一个面向真实本地仓库工作的 autonomous coding agent 框架。

它不是一组 prompt，也不是把 shell 随便交给模型。HCA 更像是给模型装了一套工程化运行时：它知道什么时候该问、什么时候该读代码、什么时候能改文件、什么时候必须验证、什么时候该承认卡住，以及如何把整个过程记录成可以复盘的证据。

HCA 基于 OpenAI-compatible Chat Completions API，可以接 DeepSeek、OpenAI 或其他兼容服务。它把 profile、工具权限、会话记录、上下文压缩、恢复策略、验收检查、浏览器验证和 benchmark 适配组合成一个本地 coding agent runtime。日常可以用它修 bug、重构、写测试、做 review、生成计划、构建小型 Web 应用；评测时也可以用同一套 runtime 跑 Terminal-Bench / Claw-SWE-Bench 风格任务。

## 为什么做这个

真实软件任务里，模型本身强不强只是一部分。很多失败并不是因为模型完全不会，而是因为 agent loop 不够稳：

- 没读清楚代码就开始改。
- 读到旧输出后不知道它已经失效。
- 命令失败后反复走同一条路。
- 没有验证就提前说完成。
- 一次 Docker、代理或依赖波动被误记成能力失败。
- 长任务跑完后没有成本、tokens、工具调用和失败轨迹的可靠台账。

HCA 解决的是这些“模型外面”的问题。它给模型提供一个更可靠的执行环境，让模型的能力真正落到仓库、测试和评测结果上。

## 当前效果

我更愿意把这个结果看成一件事：HCA 不是靠换一个更大的模型来变强，而是在 `deepseek-v4-flash` 这个更轻的模型上，把 agent runtime 该做的事情补齐了。

当前本地 Terminal-Bench 2.1 ledger 用完整 89 个任务做分母，强视觉任务也计入失败：

| 对比项 | 口径 | 结果 |
| --- | --- | ---: |
| HCA 本地结果 | Terminal-Bench 2.1 task ledger，`deepseek-v4-flash`，high reasoning，不是 Pro/Max | `55/89` passed，`61.8%` |
| DeepSeek 官方参考 | [DeepSeek-V4-Flash model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash) 中的 Terminal Bench 2.0 Acc，V4-Flash-Max | `56.9%` |

和 DeepSeek 官方 V4-Flash-Max 的 `56.9%` 相比，HCA 当前结果高出 `4.9` 个百分点。这里没有把它包装成严格同场 leaderboard 复现：我们的结果来自本地 Terminal-Bench 2.1 ledger，官方数字来自 DeepSeek 模型卡里的 Terminal Bench 2.0 口径。但这个对比仍然很有意义，因为 HCA 用的是 Flash high reasoning，不是 Flash Max，也不是 Pro/Max，却把完整 task 分母上的完成率推到了更高的位置。

这正是这个框架想证明的东西：模型能力只是起点，真正决定长任务能不能落地的，是 profile、工具治理、恢复策略、缓存友好上下文和可复盘的 eval ledger。HCA 做的是把这些工程层补上，让同一个模型系列在真实终端任务里更少卡死、更少误判、更稳定地走到可验证结果。

其他已记录的运行时收益：

| 方向 | 结果 |
| --- | --- |
| DeepSeek context cache warmup | `29.2% -> 99.1%` |
| Memory A/B suite | tool calls `-50.0%`，elapsed `-18.8%`，tokens `-44.7%` |
| Latency smoke | turn p95 `22542ms`，LLM response p95 `7983ms`，TTFT p95 `3348ms` |

当前人类可读评估结果见 [eval/results/SUMMARY.md](eval/results/SUMMARY.md)。重新从 raw artifacts 汇总：

```bash
python eval/scripts/rebuild_eval_results.py --results-root eval/results --jobs-root jobs
```

## 它适合做什么

HCA 适合需要 agent 真正在本地项目里工作的场景：

- 自动修 bug、补测试、跑验证，并解释改动。
- 读仓库后回答架构、调用链、配置和行为问题。
- 在改代码前产出可执行的实施计划。
- 做只读代码审查，优先输出风险和 bug。
- 从一句需求构建可运行的 Web 应用，并用浏览器验证。
- 通过 Harbor 跑 Terminal-Bench 任务，收集成本、tokens、工具调用和失败轨迹。
- 研究新的 agent profile、middleware、tool runtime 或 benchmark adapter。

## 核心设计

HCA 的核心不是“让模型更自由”，而是“让模型在正确边界里更自主”。

| 组件 | 作用 |
| --- | --- |
| Profile | 定义当前任务是什么：普通问答、代码实施、应用构建、计划、评审或 benchmark |
| Tool runtime | 提供文件、搜索、shell、web、子 agent、浏览器、MCP 等工具，并统一权限和并行策略 |
| Middleware | 处理循环检测、错误恢复、任务跟踪、验收检查、时间预算和退出前验证 |
| Workspace service | 约束文件读写范围，拒绝敏感路径，必要时创建快照 |
| Session log | 记录事件、观察、工具结果、LLM usage 和 checkpoint，方便复盘 |
| Eval ledger | 从 raw 结果重建 task-level 真相，避免单次中断或环境波动污染总结果 |

一句话概括：模型负责判断和生成，HCA 负责给它一个可治理、可验证、可复盘的工作现场。

## 项目结构

```text
.
├── harness_code_agent/     # 核心 Python 包
│   ├── cli.py              # `hca` 命令行入口
│   ├── core/               # 交互 session、路由、TUI glue
│   ├── agent/              # conversation state、trace、上下文压缩、provider 适配
│   ├── runtime/            # 工具、权限、middleware、approval
│   ├── workspace/          # 路径保护、快照、持久 shell
│   ├── sessions/           # session metadata 和事件日志
│   ├── skills/             # skill registry
│   └── profiles/           # general、coding-agent、app-builder、plan、review 等模式
├── skills/                 # agent 可按需读取的本地技能说明
├── eval/                   # 评估脚本、任务集、结果、benchmark adapter
│   ├── scripts/            # 基础指标、Terminal-Bench、Claw-SWE-Bench runner
│   ├── tasks/              # 固定轻量评估任务配置
│   ├── benchmarks/         # Terminal-Bench launcher 和 Harbor adapter
│   └── results/            # raw artifacts 与生成报告
└── tests/                  # unittest 测试
```

## 安装

环境要求：

- Python 3.10+
- Git
- 一个 OpenAI-compatible API key
- 可选：Playwright Chromium，用于浏览器验证
- 可选：Docker 和 Harbor，用于 Terminal-Bench

安装项目：

```bash
cd harness-code-agent
pip install -e .
```

如果只想在源码目录临时运行，也可以安装 `requirements.txt` 后使用：

```bash
python -m harness_code_agent.cli
```

但推荐 editable install，因为它会注册 `hca` 命令。

如果要使用 `app-builder` 的浏览器验证：

```bash
python -m playwright install chromium
```

创建 `.env`：

```bash
cp .env.template .env
```

Windows PowerShell：

```powershell
Copy-Item .env.template .env
```

DeepSeek 示例配置：

```env
OPENAI_API_KEY=sk-your-deepseek-key-here
OPENAI_BASE_URL=https://api.deepseek.com
HARNESS_MODEL=deepseek-v4-flash
HARNESS_MODEL_INTENSITY=normal
# HARNESS_MODEL_FAST=deepseek-v4-flash
# HARNESS_MODEL_NORMAL=deepseek-v4-flash
# HARNESS_MODEL_HARD=deepseek-v4-pro
# HARNESS_MODEL_MAX=deepseek-v4-pro
```

DeepSeek 默认档位：

| Intensity | 默认行为 |
| --- | --- |
| `fast` | `deepseek-v4-flash`，不启用 thinking |
| `normal` | `deepseek-v4-flash`，high reasoning |
| `hard` | `deepseek-v4-pro`，high reasoning |
| `max` | `deepseek-v4-pro`，max reasoning |

## 快速开始

进入交互式 TUI：

```bash
hca
```

然后输入任务，例如：

```text
修复 parser 测试失败，并说明你改了什么
```

常用命令：

```text
/doctor       检查本地配置
/config show  查看当前解析后的运行时配置
/profiles     查看产品可见 profile
/help         查看命令和可用 workflow
```

也可以启动时直接提交任务：

```bash
hca "Fix the failing tests"
```

脚本或 CI 风格入口：

```bash
hca -p "Fix the failing tests"
hca --print "Fix the failing tests"
echo "Review this repo for obvious bugs" | hca
```

常用 TUI 快捷键：

```text
Enter         提交输入
Shift+Enter   插入换行
Tab           接受补全
Esc           关闭补全
Ctrl-C        取消当前 turn
Ctrl-T        切换 thought 元信息显示
Ctrl-K        手动压缩上下文
Ctrl-P        切换权限模式
```

## Profiles

Profile 是 HCA 的主要工作模式。它决定 agent 当前是在回答问题、改代码、做计划、做 review、构建应用，还是跑 benchmark。

| Profile | 用途 |
| --- | --- |
| `general` | 默认轻量入口，用于普通问答和只读仓库检查 |
| `coding-agent` | 本地仓库主要实施模式，负责修改、测试和验证 |
| `app-builder` | 构建可运行 Web 应用，并进行浏览器验证 |
| `plan` | 只调查和写计划，不直接改代码 |
| `review` | 只读代码审查，按 findings-first 结构输出 |

`terminal` profile 专门给 Terminal-Bench / Harbor 使用。它可以通过 eval runner 或显式 `--profile terminal` 启动，但不会出现在普通产品 profile 列表里，也不会被自动路由选中。

指定 profile：

```bash
hca --profile coding-agent "Fix the TypeError in parse_config()"
hca --profile plan "Design the parser migration"
hca --profile review "Review the current branch"
hca --profile terminal "Fix the broken symlinks in /tmp"
```

TUI 中可用短命令：

```text
/general
/code
/plan
/app
/review
```

`plan` profile 有显式 handoff：它写完计划后会停住。用户回复“继续”“执行”“开始”这类短确认时，HCA 会切换到 `coding-agent`，并把刚才的 Markdown 计划注入为 approved plan。

## Skills

Skills 采用渐进式披露。HCA 不会把所有长规则常驻塞进 prompt，而是先给模型一个精简 catalog；当任务需要某个 skill 时，再用 `read_skill_file` 读取完整说明。

- 用户可直接调用的 workflow 暴露为 slash command，例如 `/implement`、`/triage`、`/handoff`、`/workflows`。
- 面向 agent 的工程纪律只放 name、description、path，相关时再按需加载正文。
- 文件和历史 session 用 `@file:`、`@session:` 明确引用。

这样既保留了专业工作流，又不会把上下文窗口浪费在暂时用不上的长文档上。

## 会话、快照和复盘

交互模式默认在启动 `hca` 的当前目录工作。HCA 会在 `.harness/` 下记录：

- session metadata
- event logs
- observations
- file snapshots
- checkpoint 信息

常用命令：

```text
/sessions
/session <session-id>
/resume <session-id>
/fork <session-id>
/rollback <session-id> <path>
/checkpoint status
/checkpoint auto off
/compact show
```

兼容的非交互命令：

```bash
hca session show latest
```

在任务里引用文件或历史 session：

```text
根据 @README.md 修复文档里的启动示例
继续 @session:20260518-120000-abcd1234 里的工作
根据 @"docs/path with spaces.md" 补充测试说明
```

## 工具运行时

HCA 的工具不是简单丢给模型自由调用，而是经过权限、lane、审批和 middleware 管理。

内置工具包括：

- repository search 和 bounded file read
- workspace-scoped 文件写入
- structured patch
- persistent shell jobs
- web search / fetch
- delegated agents for exploration, review, verification, test design, and isolated patch proposals
- 并行安全命令和并行只读 delegated agents
- 用户选择提问
- 可选浏览器验证
- MCP server 暴露的 tools

Runtime 还会注入循环检测、错误提示、恢复探针、任务跟踪、时间预算、验收检查和退出前验证。这部分是 HCA 的关键价值之一：模型可以尝试，但尝试必须留下证据，也必须能被运行时纠偏。

## 权限模式

| 模式 | 行为 |
| --- | --- |
| `workspace-write` | 默认模式。允许安全读取和工作区内受控写入；risky shell 和未知工具需要批准；极危命令永远阻断 |
| `llm-auto` | 与 `workspace-write` 同范围，但需要人工批准的调用交给 fast model 判断；低置信或失败默认拒绝 |
| `danger-full-access` | 放行非黑名单工具调用，适合受控 benchmark 环境；极危命令仍然阻断 |

文件写入通过 `WorkspaceService` 做路径检查，默认拒绝写出工作区，也拒绝 `.git/` 和敏感 `.env` 文件。

受限调查或方案设计请使用 `--profile plan`。它只暴露必要读工具、只读 shell、用户提问、子 agent 咨询和计划状态更新。

## Docker Shell Sandbox

设置 `HARNESS_SANDBOX_MODE=docker` 后，`run_bash` 会在按 session 懒启动并复用的 Docker 容器中执行。当前工作区会挂载到容器 `/workspace`，命令固定走 Linux Bash；即使宿主机是 Windows，也应使用 Bash 语法。

```env
HARNESS_SANDBOX_MODE=docker
HARNESS_DOCKER_IMAGE=python:3.12
HARNESS_DOCKER_NETWORK=none
```

默认网络是 `none`，适合隔离不可信命令。需要安装依赖时再显式改成 `bridge`。

安全边界：

- 工作区挂载为读写，容器命令可以修改项目文件。
- POSIX 主机默认使用当前用户的 `uid:gid`，避免 root-owned 文件。
- Windows Docker Desktop 不强制 UID 映射，以保持兼容。
- 文件类工具仍在宿主侧通过 `WorkspaceService` 执行；只有 shell 命令进入容器。
- 如果要跑完全不可信工作负载，建议使用外部 VM 或更强 Docker 隔离策略。

## MCP Client

HCA 可以连接 MCP server，把 server tools 暴露给当前 profile。第一版支持 `stdio` 和 `streamable_http` transports。

工作区本地配置位于 `.harness/mcp.json`：

```json
{
  "servers": {
    "docs": {
      "enabled": true,
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ${DOCS_MCP_TOKEN}" },
      "permission": "network_read"
    },
    "local_fs": {
      "enabled": true,
      "transport": "stdio",
      "command": "python",
      "args": ["server.py"],
      "env": { "TOKEN": "${LOCAL_TOKEN}" },
      "permission": "dangerous",
      "tool_permissions": { "search": "read" }
    }
  }
}
```

MCP tools 会命名为 `mcp__{server}__{tool}`，避免和内置工具冲突。

```text
/mcp status
/mcp list
/mcp reload
/doctor
```

## Evaluation

评估代码在 `eval/` 下。

常用命令：

```bash
python eval/scripts/run_basic_metrics_eval.py --dry-run
python eval/scripts/run_basic_metrics_eval.py --suites memory,latency
python eval/scripts/run_terminal_bench_eval.py --dry-run
python eval/scripts/run_terminal_bench_eval.py --tbench-task-set 24task
python eval/scripts/rebuild_eval_results.py --results-root eval/results --jobs-root jobs
```

运行单个 Terminal-Bench task：

```bash
python eval/benchmarks/run_terminal_bench.py --task fix-git
```

运行多个 task：

```bash
python eval/benchmarks/run_terminal_bench.py --task fix-git --task query-optimize
```

运行本地完整任务列表：

```bash
python eval/benchmarks/run_terminal_bench.py --full
```

使用 Daytona：

```bash
python eval/benchmarks/run_terminal_bench.py --task fix-git --env daytona
```

Eval ledger 会从 raw `summary.json`、Harbor `result.json`、HCA artifacts、stdout、stderr 里重建 task-level 结果。它比单次 run summary 更适合作为最终报告，因为它能合并多次 rerun，区分任务成功、agent timeout、verifier failure、infra/setup failure，并保留成本、tokens、工具调用和失败轨迹。

更多说明：

- [eval/README.md](eval/README.md)
- [eval/benchmarks/README.md](eval/benchmarks/README.md)
- [eval/results/SUMMARY.md](eval/results/SUMMARY.md)

## 配置项

核心配置来自 `.env` 或环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 空 | API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API 地址 |
| `HARNESS_MODEL` | `gpt-4o` | 全局兜底模型名 |
| `HARNESS_MODEL_INTENSITY` | `hard` | 主 agent 强度：`fast` / `normal` / `hard` / `max` |
| `HARNESS_MODEL_FAST` | provider 默认 | 覆盖 `fast` 档模型 |
| `HARNESS_MODEL_NORMAL` | provider 默认 | 覆盖 `normal` 档模型 |
| `HARNESS_MODEL_HARD` | provider 默认 | 覆盖 `hard` 档模型 |
| `HARNESS_MODEL_MAX` | provider 默认 | 覆盖 `max` 档模型 |
| `HARNESS_PROVIDER` | `auto` | `auto` / `openai` / `deepseek` / `openai-compatible` |
| `HARNESS_STREAM` | `auto` | streaming：`auto` / `1` / `0` |
| `HARNESS_WINDOWS_SHELL` | `auto` | Windows shell：`auto` / `pwsh` / `powershell` / `cmd` |
| `HARNESS_PERMISSION_MODE` | `workspace-write` | 权限模式 |
| `HARNESS_SANDBOX_MODE` | `host` | Shell sandbox：`host` / `docker` |
| `HARNESS_DOCKER_IMAGE` | `python:3.12` | Docker sandbox 镜像 |
| `HARNESS_DOCKER_NETWORK` | `none` | Docker 网络：`none` / `bridge` |
| `HARNESS_DOCKER_USER` | 空 | Docker user override |
| `HARNESS_CONTEXT_WINDOW_TOKENS` | `200000` | 上下文窗口估算值 |
| `COMPRESS_THRESHOLD` | `170000` | 自动压缩阈值 |
| `MAX_AGENT_ITERATIONS` | `60` | 单个 agent loop 最大迭代数 |
| `MAX_AGENT_TOTAL_TOKENS` | `0` | 单 turn token 预算；`0` 表示不启用本地 token 限制 |
| `MAX_AGENT_TOOL_CALLS` | `200` | 单 turn 工具调用预算 |
| `AGENT_BUDGET_WARN_FRACTION` | `0.8` | 预算提醒阈值 |
| `HARNESS_TRACE_STDERR` | 空 | 为 true 时输出底层 API 错误追踪 |
| `MAX_HARNESS_ROUNDS` | `5` | 旧 harness loop 兼容项 |
| `PASS_THRESHOLD` | `7.0` | 旧 harness loop 兼容项 |

Profile 参数可通过环境变量覆盖：

```bash
PROFILE_<PROFILE_NAME>_<KEY>=value
```

示例：

```bash
PROFILE_TERMINAL_TASK_BUDGET=1800
PROFILE_TERMINAL_TIME_WARN_THRESHOLD=0.45
PROFILE_CODING_AGENT_REQUIRE_START_AFTER_N_ACTIONS=5
PROFILE_APP_BUILDER_ACCEPTANCE_REVIEW_TIMEOUT=10
```

## 测试

运行完整 unittest：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

运行常用 focused suites：

```bash
python -m unittest tests.test_profiles
python -m unittest tests.test_product_runtime
python -m unittest tests.test_eval_ledger
python -m unittest tests.test_terminal_bench_launcher
```

## 开发指南

新增 profile：

1. 在 `harness_code_agent/profiles/` 下新增 `BaseProfile` 实现。
2. 实现 `name()`、`description()`、`main_agent()`。
3. 在 `harness_code_agent/profiles/__init__.py` 中注册。
4. 只有产品可见 profile 才加入 `PRODUCT_PROFILES`。
5. 为关键行为补测试。

新增内置工具：

1. 在 `harness_code_agent/runtime/builtins/` 下按领域实现工具函数。
2. 在 `harness_code_agent/runtime/builtins/schemas.py` 中声明 schema。
3. 在 registry 中注册 handler 和 permission class。
4. 只有现有权限分类表达不了风险时，才新增分类。
5. 覆盖成功路径和失败路径测试。

新增 middleware：

1. 先明确它拦截的是 tool call、tool result、loop iteration 还是 pre-exit。
2. 真正的约束写在代码里，不只写 prompt。
3. 关键行为发事件，方便 debug 和复盘。
4. 用回归测试覆盖它要防的具体失败模式。

`harness_code_agent/runtime/tools.py` 和 `harness_code_agent/runtime/middlewares.py` 只保留旧 public API 的兼容 re-export，新代码应放在结构化模块里。

## 注意事项

- 不要提交 `.env`，使用 `.env.template` 作为模板。
- `HARNESS_MODEL*` 必须是目标 provider 可识别的模型名。
- `app-builder` 的浏览器验证依赖 Playwright。
- `terminal` profile 是 eval-only 默认定位，不应进入普通产品自动路由。
- Terminal-Bench runner 会设置 `HARNESS_PERMISSION_MODE=danger-full-access` 和 `HCA_TERMINAL_EVAL_MODE=1`，以允许容器内绝对路径写入，同时保留破坏性命令保护。
- 默认 `workspace-write` 会对 risky shell 和未知工具触发批准流程。
- 如果 eval run 被中断，要报告为 interrupted 或 incomplete，不要把局部结果包装成最终 benchmark 数字。
