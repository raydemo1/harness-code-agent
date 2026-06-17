# Resume-Ready Agent Eval Report

## One-Page Metrics

| Metric | Result |
| --- | --- |
| DeepSeek context cache | warmup 29.2% -> 99.1% |
| Memory A/B | 5 tasks; tool calls -50.0%, elapsed -18.8%, tokens -44.7% |
| Latency p95/p99 | turn p95=22542ms p99=22542ms; LLM p95=7983ms; TTFT p95=3348ms |
| Terminal-Bench 2.0 8-task subset | 3/8 passed (37.5%), 8task; categories: data-processing 1/1, data-science 0/1, debugging 0/3, file-operations 1/1, software-engineering 1/2; tokens=2944532, turns=5, tools=188, est. cost=$0.0537 |
| Claw-SWE-Bench | not run |

## Mechanism Effects

- Cache: warmup 29.2% -> 99.1%
- Memory: 5 tasks; tool calls -50.0%, elapsed -18.8%, tokens -44.7%
- Compaction: rewrite diagnosed via log_rewrite; post-rewrite hit 83.2%

## Terminal-Bench Per-Task Telemetry

| Task | Status | Category | Difficulty | Elapsed | Tokens | Turns | Tools | Est. Cost |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| fix-git | failed | software-engineering | easy | 33.4s | not captured | not captured | not captured | not captured |
| overfull-hbox | failed | debugging | easy | 573.1s | 604109 | 1 | 37 | $0.0106 |
| build-cython-ext | failed | debugging | medium | 276.2s | 162471 | 0 | 24 | $0.0046 |
| custom-memory-heap-crash | failed | debugging | medium | 150.5s | not captured | not captured | not captured | not captured |
| git-leak-recovery | passed | software-engineering | medium | 190.1s | 80503 | 1 | 17 | $0.0014 |
| log-summary-date-ranges | passed | data-processing | medium | 128.6s | 100948 | 1 | 11 | $0.0022 |
| large-scale-text-editing | passed | file-operations | medium | 534.3s | 239590 | 1 | 26 | $0.0072 |
| query-optimize | failed | data-science | medium | 799.3s | 1756911 | 1 | 73 | $0.0277 |

_`not captured` means no complete HCA session metrics were available for that task._

## Resume Bullets

- Built a lightweight evaluation harness for a local coding agent, with fixed task definitions for DeepSeek cache efficiency, memory A/B, latency, and Terminal-Bench subsets.
- Measured DeepSeek prompt-cache warmup from cold start to 99.1% on stable multi-turn project context.
- Measured memory-enabled runs against disabled baselines, reporting tool-call, elapsed-time, and token deltas.
- Reported latency p50/p95/p99 from completed evaluation runs.
- Reported Terminal-Bench 2.0 8-task subset pass-rate results from completed benchmark runs.
