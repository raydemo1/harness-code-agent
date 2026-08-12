# Eval

`eval/` is the single home for evaluation code, benchmark adapters, fixed task sets, and generated results.

## Directory Map

```text
eval/
├── scripts/       # Evaluation runners and ledger rebuild tools
├── tasks/         # Fixed lightweight task definitions used by scripts
├── benchmarks/    # External benchmark adapters: Terminal-Bench 2.1 via Harbor, Claw-SWE-Bench
└── results/       # Timestamped raw outputs plus ledger.json/results.json/SUMMARY.md
```

## File Relationships

- `scripts/run_basic_metrics_eval.py` runs local basic metrics suites: memory and latency by default; cache is available when explicitly selected.
- `scripts/run_terminal_bench_eval.py` runs the fixed Terminal-Bench subsets and delegates each task to `benchmarks/run_terminal_bench.py`.
- `scripts/run_claw_swe_bench_eval.py` runs Claw-SWE-Bench through the local harness adapter.
- `scripts/deepseek_context_eval.py` runs the DeepSeek context/cache scenarios and writes timestamped outputs under `results/`.
- `scripts/rebuild_eval_results.py` scans raw `summary.json`, Harbor `result.json`, VeriForge artifacts, stdout, and stderr to regenerate `results/ledger.json`, `results/results.json`, `results/retention_plan.json`, and `results/SUMMARY.md`.
- `tasks/memory_ab.json`, `tasks/latency_smoke.json`, `tasks/terminal_bench_8task.json`, and `tasks/terminal_bench_24task.json` define the lightweight interview-project eval workload.
- `benchmarks/run_terminal_bench.py` launches the fixed Terminal-Bench subset through Harbor.
- `benchmarks/run_claw_swe_bench.py` launches Claw-SWE-Bench through the upstream orchestrator with the local harness adapter.
- `benchmarks/harbor_agent.py` is Harbor's installed-agent adapter for running VeriForge with the `terminal` profile inside task containers.
- `benchmarks/harness_claw_adapter.py` runs VeriForge with the `coding-agent` profile inside each Claw-SWE-Bench container and lets the upstream runner collect patches. Claw remains an external evaluation scenario rather than a separate product profile.
- `benchmarks/tb2_tasks.json` stores Terminal-Bench task metadata used by the terminal profile and launcher.

## Common Commands

```bash
python eval/scripts/run_basic_metrics_eval.py --dry-run
python eval/scripts/run_basic_metrics_eval.py --suites memory,latency
python eval/scripts/run_basic_metrics_eval.py --suites cache
python eval/scripts/run_basic_metrics_eval.py --suites latency
python eval/scripts/run_terminal_bench_eval.py --dry-run
python eval/scripts/run_terminal_bench_eval.py --tbench-task-set 24task
python eval/scripts/run_claw_swe_bench_eval.py --dry-run --claw-limit 5
python eval/scripts/run_claw_swe_bench_eval.py --claw-limit 5
python eval/scripts/rebuild_eval_results.py --results-root eval/results --jobs-root jobs
python eval/benchmarks/run_terminal_bench.py --task fix-git
python eval/benchmarks/run_claw_swe_bench.py --limit 5
```

Claw-SWE-Bench uses the Hugging Face dataset `TokenRhythm/Claw-SWE-Bench`
with the `lite` config by default. It requires Docker SWE-bench images and the
optional host dependency `datasets`; the launcher will clone the upstream
`opensquilla/claw-swe-bench` orchestrator into `.harbor/datasets/` on first use.

Use `eval/results/SUMMARY.md` as the human-facing summary. It should only contain metrics produced from real runs.
The current Terminal-Bench 2.1 ledger reports `56/89` passed (`62.9%`)
with `DeepSeek-V4-Flash-Preview` through VeriForge.
The official reference is `61.8%` for the same model on Terminal-Bench 2.1 at
Max reasoning intensity with the official DeepSeek Harness.
