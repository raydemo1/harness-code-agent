# Agent Eval Internal Report

Generated at: 2026-06-17T15:31:51
Result root: eval\results

## Sources
- eval\results\2026-06-17_013123_latency
- eval\results\2026-06-17_151752_memory_ab
- eval\results\2026-06-17_152258_latency
- eval\results\2026-06-17_153037_tbench

## Metrics

| Area | Key Result |
| --- | --- |
| Context cache | not run |
| Memory A/B | 5 tasks; tool calls -27.5%, elapsed --52.2%, tokens -20.2% |
| Latency | turn p95=27475ms p99=27475ms; LLM p95=8527ms; TTFT p95=4357ms |
| Terminal-Bench 2.0 8-task subset | 0/8 passed (0.0%), 8task; categories: data-processing 0/1, data-science 0/1, debugging 0/3, file-operations 0/1, software-engineering 0/2 |
| Claw-SWE-Bench | not run |
