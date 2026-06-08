# DeepSeek Context Cache Eval

Created at: 2026-06-08T15:33:56

## Scenario Summary

| Scenario | Turns | Avg Hit Ratio | First -> Last | Prefix Changes |
| --- | ---: | ---: | ---: | --- |
| compaction_rewrite | 4 | 59.6% | 99.9% -> 66.3% | after_rewrite_turn_1:log_rewrite |

## Calls

| Scenario | Label | Kind | Prompt | Hit | Miss | Hit Ratio | Prefix Change |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| compaction_rewrite | before_rewrite | measured | 17676 | 17664 | 12 | 99.9% | none |
| compaction_rewrite | rewrite_summary_call | summarizer | 1798 | 1792 | 6 | 99.7% | none |
| compaction_rewrite | after_rewrite_turn_1 | measured | 322 | 0 | 322 | 0.0% | log_rewrite |
| compaction_rewrite | after_rewrite_turn_2 | measured | 354 | 256 | 98 | 72.3% | none |
| compaction_rewrite | after_rewrite_turn_3 | measured | 386 | 256 | 130 | 66.3% | none |
