# DeepSeek Context Cache Eval

Created at: 2026-06-08T15:53:50

## Scenario Summary

| Scenario | Turns | Avg Hit Ratio | First -> Last | Prefix Changes |
| --- | ---: | ---: | ---: | --- |
| compaction_rewrite | 13 | 80.6% | 99.9% -> 89.2% | after_rewrite_turn_1:log_rewrite |

## Calls

| Scenario | Label | Kind | Prompt | Hit | Miss | Hit Ratio | Prefix Change |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| compaction_rewrite | before_rewrite | measured | 17676 | 17664 | 12 | 99.9% | none |
| compaction_rewrite | rewrite_summary_call | summarizer | 1798 | 1792 | 6 | 99.7% | none |
| compaction_rewrite | after_rewrite_turn_1 | measured | 530 | 0 | 530 | 0.0% | log_rewrite |
| compaction_rewrite | after_rewrite_turn_2 | measured | 583 | 512 | 71 | 87.8% | none |
| compaction_rewrite | after_rewrite_turn_3 | measured | 626 | 512 | 114 | 81.8% | none |
| compaction_rewrite | after_rewrite_turn_4 | measured | 668 | 512 | 156 | 76.6% | none |
| compaction_rewrite | after_rewrite_turn_5 | measured | 710 | 640 | 70 | 90.1% | none |
| compaction_rewrite | after_rewrite_turn_6 | measured | 752 | 640 | 112 | 85.1% | none |
| compaction_rewrite | after_rewrite_turn_7 | measured | 794 | 640 | 154 | 80.6% | none |
| compaction_rewrite | after_rewrite_turn_8 | measured | 836 | 768 | 68 | 91.9% | none |
| compaction_rewrite | after_rewrite_turn_9 | measured | 878 | 768 | 110 | 87.5% | none |
| compaction_rewrite | after_rewrite_turn_10 | measured | 920 | 768 | 152 | 83.5% | none |
| compaction_rewrite | after_rewrite_turn_11 | measured | 962 | 896 | 66 | 93.1% | none |
| compaction_rewrite | after_rewrite_turn_12 | measured | 1004 | 896 | 108 | 89.2% | none |
