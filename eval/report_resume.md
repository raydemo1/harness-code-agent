# Resume-Ready Agent Eval Report

## One-Page Metrics

| Metric | Result |
| --- | --- |
| DeepSeek context cache | not run |
| Memory A/B | 5 tasks; tool calls -27.5%, elapsed --52.2%, tokens -20.2% |
| Latency p95/p99 | turn p95=27475ms p99=27475ms; LLM p95=8527ms; TTFT p95=4357ms |
| Terminal-Bench 2.0 8-task subset | 0/8 passed (0.0%), 8task; categories: data-processing 0/1, data-science 0/1, debugging 0/3, file-operations 0/1, software-engineering 0/2 |
| Claw-SWE-Bench | not run |

## Mechanism Effects

- Cache: not run
- Memory: 5 tasks; tool calls -27.5%, elapsed --52.2%, tokens -20.2%
- Compaction: not run

## Resume Bullets

- Built a lightweight evaluation harness for a local coding agent, with fixed task definitions for DeepSeek cache efficiency, memory A/B, latency, and Terminal-Bench subsets.
- Measured memory-enabled runs against disabled baselines, reporting tool-call, elapsed-time, and token deltas.
- Reported latency p50/p95/p99 from completed evaluation runs.
- Reported Terminal-Bench 2.0 8-task subset pass-rate results from completed benchmark runs.
