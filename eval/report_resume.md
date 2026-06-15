# Resume-Ready Agent Eval Report

## One-Page Metrics

| Metric | Result |
| --- | --- |
| DeepSeek context cache | warmup 0.0% -> 98.6% |
| Memory A/B | not run |
| Latency p95/p99 | not run |
| Terminal-Bench 2.0 subset | not run |

## Mechanism Effects

- Cache: warmup 0.0% -> 98.6%
- Memory: not run
- Compaction: rewrite diagnosed via log_rewrite; post-rewrite hit 91.5%

## Resume Bullets

- Built a lightweight evaluation harness for a local coding agent, with fixed task definitions for DeepSeek cache efficiency, memory A/B, latency, and Terminal-Bench subsets.
- Measured DeepSeek prompt-cache warmup from cold start to 98.6% on stable multi-turn project context.
