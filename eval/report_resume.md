# Resume-Ready Agent Eval Report

## One-Page Metrics

| Metric | Result |
| --- | --- |
| DeepSeek context cache | warmup 0.0% -> 99.0% |
| Memory A/B | 5 tasks; tool calls -82.2%, elapsed -51.7%, tokens -90.7% |
| Latency p95/p99 | turn p95=96329ms p99=96329ms; LLM p95=11160ms; TTFT p95=5855ms |
| Terminal-Bench 2.0 subset | not run |
| Claw-SWE-Bench | not run |

## Mechanism Effects

- Cache: warmup 0.0% -> 99.0%
- Memory: 5 tasks; tool calls -82.2%, elapsed -51.7%, tokens -90.7%
- Compaction: rewrite diagnosed via log_rewrite; post-rewrite hit 83.9%

## Resume Bullets

- Built a lightweight evaluation harness for a local coding agent, with fixed task definitions for DeepSeek cache efficiency, memory A/B, latency, and Terminal-Bench subsets.
- Measured DeepSeek prompt-cache warmup from cold start to 99.0% on stable multi-turn project context.
- Measured memory-enabled runs against disabled baselines, reporting tool-call, elapsed-time, and token deltas.
- Reported latency p50/p95/p99 from completed evaluation runs.
