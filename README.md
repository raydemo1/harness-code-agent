# VeriForge

VeriForge — Verifiable Coding-Agent Runtime

VeriForge 是一个面向真实代码仓库的 coding-agent runtime，提供 profile、工具权限、会话、上下文、恢复、验收和评测能力。

项目基于 OpenAI-compatible Chat Completions API，可接入 DeepSeek、OpenAI 等兼容服务。它支持修 bug、补测试、做 review、写计划、构建小型 Web 应用，以及运行 Terminal-Bench 和 Claw-SWE-Bench 风格评测。

## TUI 预览

![VeriForge OpenTUI：Mac 三点窗口、任务清单、工具调用、输入框与运行状态](https://raw.githubusercontent.com/raydemo1/veriforge-agent/main/docs/images/veriforge-tui.png)

终端界面展示任务计划、当前 profile 和运行状态；工具调用、恢复过程和验收结果统一归档到 session。

## 为什么做这个

真实软件任务里，失败经常来自 agent loop 本身，而不只是模型能力：

- 没读清楚代码就开始改。
- 读到旧输出后不知道它已经失效。
- 命令失败后反复走同一条路。
- 没有验证就提前说完成。
- 一次 Docker、代理或依赖波动被误记成能力失败。
- 长任务跑完后没有成本、tokens、工具调用和失败轨迹的可靠台账。

VeriForge 把这些边界放进 runtime：什么时候读代码、什么时候改文件、什么时候重试、什么时候验收，都有对应的工具和 middleware 负责。

## 一次完整评测

| 对比项 | 口径 | 结果 |
| --- | --- | ---: |
| VeriForge 本地结果 | `DeepSeek-V4-Flash-Preview` + VeriForge，Terminal-Bench 2.1 task ledger | `56/89` passed，`62.9%` |
| DeepSeek 官方参考 | `DeepSeek-V4-Flash-Preview` + 官方 DeepSeek Harness，Terminal-Bench 2.1，Max reasoning | `61.8%` |

本地结果来自 VeriForge，官方参考来自 DeepSeek Harness；两者使用同一个模型版本和 Terminal-Bench 2.1 评测集，但推理强度按各自运行配置记录。

项目重点是模型之外的 profile、工具治理、失败恢复、上下文管理和评测账本。

其他运行记录：

| 方向 | 结果 |
| --- | --- |
| DeepSeek context cache warmup | `29.2% -> 99.1%` |
| Memory A/B suite | tool calls `-50.0%`，elapsed `-18.8%`，tokens `-44.7%` |
| Latency smoke | turn p95 `22542ms`，LLM response p95 `7983ms`，TTFT p95 `3348ms` |

人类可读的评估摘要在 [eval/results/SUMMARY.md](eval/results/SUMMARY.md)。需要重新汇总时运行：

```bash
python eval/scripts/rebuild_eval_results.py --results-root eval/results --jobs-root jobs
```

## 能拿来做什么

它主要用于这些场景：

- 自动修 bug、补测试、跑验证，并解释改动。
- 读仓库后回答架构、调用链、配置和行为问题。
- 在改代码前产出可执行的实施计划。
- 做只读代码审查，优先输出风险和 bug。
- 从一句需求构建可运行的 Web 应用，并用浏览器验证。
- 通过 Harbor 跑 Terminal-Bench 任务，收集成本、tokens、工具调用和失败轨迹。
- 研究新的 agent profile、middleware、tool runtime 或 benchmark adapter。

## 核心设计

模型负责判断和生成，runtime 负责把工具、权限、状态和验收串起来。

| 组件 | 作用 |
| --- | --- |
| Profile | 定义当前任务是什么：普通问答、代码实施、应用构建、计划、评审或 benchmark |
| Tool runtime | 提供文件、搜索、shell、web、子 agent、浏览器、MCP 等工具，并统一权限和并行策略 |
| Middleware | 处理循环检测、错误恢复、任务跟踪、验收检查、时间预算和退出前验证 |
| Workspace service | 约束文件读写范围，拒绝敏感路径，必要时创建快照 |
| Session log | 记录事件、观察、工具结果、LLM usage 和 checkpoint，方便复盘 |
| Eval ledger | 从 raw 结果重建 task-level 真相，避免单次中断或环境波动污染总结果 |

一句话概括：让 agent 在真实仓库里工作，并对结果进行验证。

## 项目结构

```text
.
├── harness_code_agent/     # 核心 Python 包
│   ├── cli.py              # `veriforge` 命令行入口
│   ├── opentui_launcher.py # Bun/OpenTUI 子进程启动器
│   ├── tui_bridge.py       # Python runtime 与前端之间的 NDJSON bridge
│   ├── core/               # 交互 session、路由、TUI glue
│   ├── agent/              # conversation state、trace、上下文压缩、provider 适配
│   ├── runtime/            # 工具、权限、middleware、approval
│   ├── workspace/          # 路径保护、快照、Shell 与后台任务
│   ├── sessions/           # session metadata 和事件日志
│   ├── skills/             # skill registry 与按需加载的 catalog
│   └── profiles/           # general、coding-agent、app-builder、plan、review 等模式
├── frontend/opentui/       # Bun + React + TypeScript 的终端界面
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
- Bun 1.4+（OpenTUI 前端）
- Git
- 一个 OpenAI-compatible API key
- 可选：Playwright Chromium，用于浏览器验证
- 可选：Docker 和 Harbor，用于 Terminal-Bench

安装项目：

```bash
cd veriforge-agent
pip install -e .

# 安装 OpenTUI 前端依赖（首次运行前执行）
cd frontend/opentui
bun install
cd ../..
```

源码目录运行：

```bash
python -m harness_code_agent.cli
```

editable install 会注册 `veriforge` 命令。

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
veriforge
```

交互前端使用 Bun + React + TypeScript 的 OpenTUI；Python 负责
`InteractiveSession`、工具、权限和会话持久化，二者通过本地 NDJSON 协议通信。
历史恢复、新会话、工作模式、检查点、MCP、运行观察、审批、问题选择、命令和
`@` 文件补全均在 OpenTUI 内完成。

需要保留当前终端画面而不切换 alternate screen 时：

```bash
veriforge --no-alt-screen
```

然后输入任务，例如：

```text
修复 parser 测试失败，并说明你改了什么
```

常用命令：

```text
/checkpoint   管理检查点
/mcp          管理 MCP 服务与工具
/compact      压缩当前对话上下文
/fork         从当前会话创建分支
/observe      打开运行观察
```

输入 `/` 会打开可滚动命令面板，最多显示 8 行；上下键、PageUp/PageDown、Home/End 都会保持当前选项可见。右上角的历史和新会话图标是可点击的真实入口；profile 选择直接点击底部状态栏入口完成，再切回自动模式时由本地匹配优先、fast model 兜底的路由器判断。

主题默认跟随终端，可显式指定；Nerd Font 图标需要主动启用，默认使用不会缺字的 Unicode 图标：

```bash
veriforge --theme light
veriforge --theme dark --icons nerd
```

也可以启动时直接提交任务：

```bash
veriforge "Fix the failing tests"
```

脚本或 CI 风格入口：

```bash
veriforge -p "Fix the failing tests"
veriforge --print "Fix the failing tests"
echo "Review this repo for obvious bugs" | veriforge
```

常用 TUI 操作：

```text
Enter         提交输入
Shift+Enter   插入换行
Tab           接受补全
Esc           关闭补全
Ctrl-C        取消当前 turn；空闲时退出
Ctrl-R        打开历史会话
Ctrl-N        开始新会话
Ctrl-O        打开运行观察
Ctrl-P        切换权限模式
?             输入框为空时打开帮助
鼠标点击权限  同样可以切换权限模式
```

## Profiles

Profile 是 VeriForge 的主要工作模式。它决定 agent 当前是在回答问题、改代码、做计划、做 review、构建应用，还是跑 benchmark。

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
veriforge --profile coding-agent "Fix the TypeError in parse_config()"
veriforge --profile plan "Design the parser migration"
veriforge --profile review "Review the current branch"
veriforge --profile terminal "Fix the broken symlinks in /tmp"
```

底部 profile 选择面板提供自动路由、通用、编码、规划、应用构建和审查模式。选择具体模式后会固定当前工作模式；再次选择自动路由即可交回本地路由器判断。规划完成后，仍可在当前对话中继续执行已确认的计划。

## Skills

Skills 采用渐进式披露。常驻 prompt 保留精简 catalog，任务需要时再用 `read_skill_file` 读取完整说明。

- 用户可直接调用的 workflow 暴露为 slash command，例如 `/to-spec`、`/to-tickets`、`/implement`、`/skill-creator`、`/find-skills` 和 `/workflows`。
- 面向 agent 的工程纪律只放 name、description、path，相关时再按需加载正文。
- catalog 选择性同步 Matt Pocock 的工程技能并做 VeriForge 适配；不会自动全量安装上游目录。
- 文件和历史 session 用 `@file:`、`@session:` 明确引用。

这样专业工作流可以复用，常驻 prompt 保持精简。

## 会话、快照和复盘

交互模式默认在启动 `veriforge` 的当前目录工作。VeriForge 会在 `.harness/` 下记录：

- session metadata
- event logs
- observations
- file snapshots
- checkpoint 信息

相关命令：

```text
/checkpoint
/compact
/fork
/observe
```

非交互入口：

```bash
veriforge session show latest
veriforge session observe latest
veriforge session observe project --export
```

在任务里引用文件：

```text
根据 @README.md 修复文档里的启动示例
根据 @"docs/path with spaces.md" 补充测试说明
```

## 工具运行时

工具调用统一经过权限检查、审批和并发调度。

内置工具包括：

- repository search 和 bounded file read
- workspace-scoped 文件写入
- structured patch
- 独立前台 shell 调用和可管理的后台 shell jobs
- web search / fetch
- 后台子代理与隔离 worker 提案
- 自动并行无冲突的工具调用
- 用户选择提问
- 可选浏览器验证
- MCP server 暴露的 tools

运行规则：

- 同一文件的读写按顺序执行。
- 不同文件的受控修改可以并行。
- 测试、构建和代码检查不与文件修改并行。
- `run_bash` 每次使用新的 Shell，并从 workspace 根目录开始。
- 需要共享目录或环境变量的步骤应写在同一条命令中。
- 未声明副作用的扩展工具按独占方式运行。
- 子代理跨主回合运行；补充消息在下一次迭代生效。
- worker 改动需显式审查和应用；同文件改动使用三方合并。

Runtime 还会处理循环检测、错误提示、恢复探针、任务跟踪、时间预算、验收检查和退出前验证。

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

VeriForge 可以连接 MCP server，把 server tools 暴露给当前 profile。第一版支持 `stdio` 和 `streamable_http` transports。

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
/mcp
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

Eval ledger 会从 raw `summary.json`、Harbor `result.json`、VeriForge artifacts、stdout、stderr 里重建 task-level 结果。它比单次 run summary 更适合作为最终报告，因为它能合并多次 rerun，区分任务成功、agent timeout、verifier failure、infra/setup failure，并保留成本、tokens、工具调用和失败轨迹。

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
| `HARNESS_MODEL_INPUT_MODE` | `text` | 模型输入能力：两种模式均通过内置 Skill 处理 PDF/DOCX；`multimodal` 额外原文直传 JPEG、PNG、GIF、WebP 图片 |
| `HARNESS_WINDOWS_SHELL` | `pwsh` | Windows host shell：`pwsh` / `wsl`；严格使用所选后端，不自动降级 |
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
| `MAX_HARNESS_ROUNDS` | `5` | harness loop 轮数 |
| `PASS_THRESHOLD` | `7.0` | 通过阈值 |

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

当前基线包含 28 个 Python 测试文件、519 个 unittest 用例，以及 18 个 OpenTUI/Bun 测试；两侧需要分别验证。

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

运行 OpenTUI 测试和 TypeScript 类型检查：

```bash
cd frontend/opentui
bun test
bun run check
```

## 开发指南

新增 profile：

1. 在 `harness_code_agent/profiles/` 下新增 `BaseProfile` 实现。
2. 实现 `name()`、`description()`、`main_agent()`。
3. 在 `harness_code_agent/profiles/__init__.py` 中注册。
4. 产品可见 profile 加入 `PRODUCT_PROFILES`。
5. 为关键行为补测试。

新增内置工具：

1. 在 `harness_code_agent/runtime/builtins/` 下按领域实现工具函数。
2. 在 `harness_code_agent/runtime/builtins/schemas.py` 中声明 schema。
3. 在 registry 中注册 handler 和 permission class。
4. 现有权限分类覆盖风险时直接复用，需要扩展时再新增分类。
5. 覆盖成功路径和失败路径测试。

新增 middleware：

1. 先明确它拦截的是 tool call、tool result、loop iteration 还是 pre-exit。
2. 真正的约束写在代码里，不只写 prompt。
3. 关键行为发事件，方便 debug 和复盘。
4. 用回归测试覆盖它要防的具体失败模式。

## 注意事项

- 不要提交 `.env`，使用 `.env.template` 作为模板。
- `HARNESS_MODEL*` 必须是目标 provider 可识别的模型名。
- `app-builder` 的浏览器验证依赖 Playwright。
- `terminal` profile 用于 eval runner，普通产品自动路由使用产品 profile。
- Terminal-Bench runner 会设置 `HARNESS_PERMISSION_MODE=danger-full-access` 和 `HCA_TERMINAL_EVAL_MODE=1`，以允许容器内绝对路径写入，同时保留破坏性命令保护。
- 默认 `workspace-write` 会对 risky shell 和未知工具触发批准流程。
- 如果 eval run 被中断，要报告为 interrupted 或 incomplete，不要把局部结果包装成最终 benchmark 数字。
