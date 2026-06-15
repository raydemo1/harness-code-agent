# Eval

`eval/` is the single home for evaluation code, benchmark adapters, fixed task sets, and generated results.

## Directory Map

```text
eval/
├── scripts/       # Evaluation runners and report summarizers
├── tasks/         # Fixed lightweight task definitions used by scripts
├── benchmarks/    # External benchmark adapters, currently Terminal-Bench 2.0 via Harbor
├── results/       # Timestamped raw outputs from real eval runs
├── eval_summary.json
├── report_internal.md
└── report_resume.md
```

## File Relationships

- `scripts/run_eval_suite.py` is the main entrypoint. It reads task definitions from `tasks/`, calls cache/memory/latency suites directly, and delegates Terminal-Bench runs to `benchmarks/run_terminal_bench.py`.
- `scripts/deepseek_context_eval.py` runs the DeepSeek context/cache scenarios and writes timestamped outputs under `results/`.
- `scripts/summarize_eval.py` scans `results/` and regenerates `eval_summary.json`, `report_internal.md`, and `report_resume.md`.
- `tasks/memory_ab.json`, `tasks/latency_smoke.json`, `tasks/terminal_bench_8task.json`, and `tasks/terminal_bench_24task.json` define the lightweight interview-project eval workload.
- `benchmarks/run_terminal_bench.py` launches the fixed Terminal-Bench subset through Harbor.
- `benchmarks/harbor_agent.py` is Harbor's installed-agent adapter for running `hca --profile terminal` inside task containers.
- `benchmarks/tb2_tasks.json` stores Terminal-Bench task metadata used by the terminal profile and launcher.

## Common Commands

```bash
python eval/scripts/run_eval_suite.py --dry-run
python eval/scripts/run_eval_suite.py --suites cache
python eval/scripts/run_eval_suite.py --suites tbench
python eval/scripts/run_eval_suite.py --suites tbench --tbench-task-set 24task
python eval/scripts/summarize_eval.py
python eval/benchmarks/run_terminal_bench.py --task fix-git
```

Use `eval/report_resume.md` as the interview-facing summary. It should only contain metrics produced from real runs.
