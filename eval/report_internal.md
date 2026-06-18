# Agent Eval Internal Report

Generated at: 2026-06-18T14:55:03
Result root: eval\results

## Sources
- eval\results\2026-06-17_160835_memory_ab
- eval\results\2026-06-17_153336_tbench
- eval\results\2026-06-17_171758_deepseek_context_eval_cache
- eval\results\2026-06-17_171923_latency
- eval\results\2026-06-17_174214_tbench_hca_8task_full_metrics
- eval\results\2026-06-18_002636_tbench_tbench_hca_8task_failed4_light_plan
- eval\results\2026-06-18_011317_tbench_hca_8task_latest_merged
- eval\results\2026-06-18_135415_tbench_tbench_hca_failed3_observable
- eval\results\2026-06-18_141349_tbench_tbench_hca_failed3_full_trace
- eval\results\2026-06-18_143322_tbench_hca_8task_latest_full_trace_merged
- eval\results\2026-06-18_143946_tbench_tbench_hca_framework_fix2

## Metrics

| Area | Key Result |
| --- | --- |
| Context cache | warmup 29.2% -> 99.1% |
| Memory A/B | 5 tasks; tool calls -50.0%, elapsed -18.8%, tokens -44.7% |
| Latency | turn p95=22542ms p99=22542ms; LLM p95=7983ms; TTFT p95=3348ms |
| Terminal-Bench 2.0 8-task subset | 0/2 passed (0.0%), 8task; categories: debugging 0/2; tokens=1605202, turns=2, tools=122, est. cost=$0.0267 |
| Claw-SWE-Bench | not run |

## Terminal-Bench Per-Task Telemetry

| Task | Status | Category | Difficulty | Elapsed | Tokens | Turns | Tools | Est. Cost |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| build-cython-ext | failed | debugging | medium | 458.2s | 1272269 | 1 | 88 | $0.0153 |
| custom-memory-heap-crash | failed | debugging | medium | 458.4s | 332933 | 1 | 34 | $0.0115 |

_`not captured` means the task passed, but no complete HCA session metrics were available for that task._
