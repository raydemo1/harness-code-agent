# Eval

`eval/` is the single home for evaluation code, benchmark adapters, fixed task sets, and generated results.

## Directory Map

```text
eval/
├── scripts/       # Evaluation runners and report summarizers
├── tasks/         # Fixed lightweight task definitions used by scripts
├── benchmarks/    # External benchmark adapters: Terminal-Bench 2.0 via Harbor, Claw-SWE-Bench
├── results/       # Timestamped raw outputs from real eval runs
├── eval_summary.json
├── report_internal.md
└── report_resume.md
```

## File Relationships

- `scripts/run_memory_cache_eval.py` runs local cache, memory, and latency suites by default.
- `scripts/run_terminal_bench_eval.py` runs the fixed Terminal-Bench subsets and delegates each task to `benchmarks/run_terminal_bench.py`.
- `scripts/run_claw_swe_bench_eval.py` runs Claw-SWE-Bench through the local harness adapter.
- `scripts/deepseek_context_eval.py` runs the DeepSeek context/cache scenarios and writes timestamped outputs under `results/`.
- `scripts/summarize_eval.py` scans `results/` and regenerates `eval_summary.json`, `report_internal.md`, and `report_resume.md`.
- `tasks/memory_ab.json`, `tasks/latency_smoke.json`, `tasks/terminal_bench_8task.json`, and `tasks/terminal_bench_24task.json` define the lightweight interview-project eval workload.
- `benchmarks/run_terminal_bench.py` launches the fixed Terminal-Bench subset through Harbor.
- `benchmarks/run_claw_swe_bench.py` launches Claw-SWE-Bench through the upstream orchestrator with the local harness adapter.
- `benchmarks/harbor_agent.py` is Harbor's installed-agent adapter for running `hca --profile terminal` inside task containers.
- `benchmarks/harness_claw_adapter.py` runs `hca` with the `swe-bench` profile inside each Claw-SWE-Bench container and lets the upstream runner collect patches.
- `benchmarks/tb2_tasks.json` stores Terminal-Bench task metadata used by the terminal profile and launcher.

## Common Commands

```bash
python eval/scripts/run_memory_cache_eval.py --dry-run
python eval/scripts/run_memory_cache_eval.py --suites cache,memory,latency
python eval/scripts/run_memory_cache_eval.py --suites latency
python eval/scripts/run_terminal_bench_eval.py --dry-run
python eval/scripts/run_terminal_bench_eval.py --tbench-task-set 24task
python eval/scripts/run_claw_swe_bench_eval.py --dry-run --claw-limit 5
python eval/scripts/run_claw_swe_bench_eval.py --claw-limit 5
python eval/scripts/summarize_eval.py
python eval/benchmarks/run_terminal_bench.py --task fix-git
python eval/benchmarks/run_claw_swe_bench.py --limit 5
```

Claw-SWE-Bench uses the Hugging Face dataset `TokenRhythm/Claw-SWE-Bench`
with the `lite` config by default. It requires Docker SWE-bench images and the
optional host dependency `datasets`; the launcher will clone the upstream
`opensquilla/claw-swe-bench` orchestrator into `.harbor/datasets/` on first use.

Use `eval/report_resume.md` as the interview-facing summary. It should only contain metrics produced from real runs.
