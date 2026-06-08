# DeepSeek Context Cache Eval

Created at: 2026-06-08T15:29:33

## Scenario Summary

| Scenario | Turns | Avg Hit Ratio | First -> Last | Prefix Changes |
| --- | ---: | ---: | ---: | --- |
| stable_warmup | 5 | 79.1% | 0.0% -> 98.6% | none |
| schema_reorder | 3 | 66.1% | 0.0% -> 99.2% | none |
| compaction_rewrite | 2 | 0.0% | 0.0% -> 0.0% | after_rewrite:log_rewrite |

## Calls

| Scenario | Label | Kind | Prompt | Hit | Miss | Hit Ratio | Prefix Change |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| stable_warmup | stable_warmup_turn_1 | measured | 11503 | 0 | 11503 | 0.0% | none |
| stable_warmup | stable_warmup_turn_2 | measured | 11538 | 11392 | 146 | 98.7% | none |
| stable_warmup | stable_warmup_turn_3 | measured | 11608 | 11520 | 88 | 99.2% | none |
| stable_warmup | stable_warmup_turn_4 | measured | 11643 | 11520 | 123 | 98.9% | none |
| stable_warmup | stable_warmup_turn_5 | measured | 11678 | 11520 | 158 | 98.6% | none |
| schema_reorder | canonical_a | measured | 11487 | 0 | 11487 | 0.0% | none |
| schema_reorder | canonical_a_repeat | measured | 11487 | 11392 | 95 | 99.2% | none |
| schema_reorder | reordered_b | measured | 11487 | 11392 | 95 | 99.2% | none |
| compaction_rewrite | before_rewrite | measured | 17676 | 0 | 17676 | 0.0% | none |
| compaction_rewrite | rewrite_summary_call | summarizer | 1798 | 0 | 1798 | 0.0% | none |
| compaction_rewrite | after_rewrite | measured | 105 | 0 | 105 | 0.0% | log_rewrite |
