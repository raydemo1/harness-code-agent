# Resume-Ready Agent Eval Report

## One-Page Metrics

| Metric | Result |
| --- | --- |
| DeepSeek context cache | warmup 29.2% -> 99.1% |
| Memory A/B | 5 tasks; tool calls -50.0%, elapsed -18.8%, tokens -44.7% |
| Latency p95/p99 | turn p95=22542ms p99=22542ms; LLM p95=7983ms; TTFT p95=3348ms |
| Terminal-Bench 2.0 8-task subset | 0/2 passed (0.0%), 8task; categories: debugging 0/2; tokens=1605202, turns=2, tools=122, est. cost=$0.0267 |
| Claw-SWE-Bench | not run |

## Mechanism Effects

- Cache: warmup 29.2% -> 99.1%
- Memory: 5 tasks; tool calls -50.0%, elapsed -18.8%, tokens -44.7%
- Compaction: rewrite diagnosed via log_rewrite; post-rewrite hit 83.2%

## Terminal-Bench Per-Task Telemetry

| Task | Status | Category | Difficulty | Elapsed | Tokens | Turns | Tools | Est. Cost |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| build-cython-ext | failed | debugging | medium | 458.2s | 1272269 | 1 | 88 | $0.0153 |
| custom-memory-heap-crash | failed | debugging | medium | 458.4s | 332933 | 1 | 34 | $0.0115 |

_`not captured` means the task passed, but no complete HCA session metrics were available for that task._

## Resume Bullets

- Built a lightweight evaluation harness for a local coding agent, with fixed task definitions for DeepSeek cache efficiency, memory A/B, latency, and Terminal-Bench subsets.
- Measured DeepSeek prompt-cache warmup from cold start to 99.1% on stable multi-turn project context.
- Measured memory-enabled runs against disabled baselines, reporting tool-call, elapsed-time, and token deltas.
- Reported latency p50/p95/p99 from completed evaluation runs.
- Reported Terminal-Bench 2.0 8-task subset pass-rate results from completed benchmark runs.
