# DeepSeek Context Cache Eval

Created at: 2026-06-08T15:56:57

## Scenario Summary

| Scenario | Turns | Avg Hit Ratio | First -> Last | Prefix Changes |
| --- | ---: | ---: | ---: | --- |
| compaction_rewrite | 25 | 84.8% | 99.9% -> 91.5% | after_rewrite_turn_1:log_rewrite |

## Calls

| Scenario | Label | Kind | Prompt | Hit | Miss | Hit Ratio | Prefix Change |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| compaction_rewrite | before_rewrite | measured | 17676 | 17664 | 12 | 99.9% | none |
| compaction_rewrite | rewrite_summary_call | summarizer | 1798 | 1792 | 6 | 99.7% | none |
| compaction_rewrite | after_rewrite_turn_1 | measured | 443 | 0 | 443 | 0.0% | log_rewrite |
| compaction_rewrite | after_rewrite_turn_2 | measured | 494 | 384 | 110 | 77.7% | none |
| compaction_rewrite | after_rewrite_turn_3 | measured | 540 | 384 | 156 | 71.1% | none |
| compaction_rewrite | after_rewrite_turn_4 | measured | 594 | 512 | 82 | 86.2% | none |
| compaction_rewrite | after_rewrite_turn_5 | measured | 645 | 512 | 133 | 79.4% | none |
| compaction_rewrite | after_rewrite_turn_6 | measured | 693 | 640 | 53 | 92.4% | none |
| compaction_rewrite | after_rewrite_turn_7 | measured | 739 | 640 | 99 | 86.6% | none |
| compaction_rewrite | after_rewrite_turn_8 | measured | 786 | 640 | 146 | 81.4% | none |
| compaction_rewrite | after_rewrite_turn_9 | measured | 833 | 768 | 65 | 92.2% | none |
| compaction_rewrite | after_rewrite_turn_10 | measured | 878 | 768 | 110 | 87.5% | none |
| compaction_rewrite | after_rewrite_turn_11 | measured | 925 | 768 | 157 | 83.0% | none |
| compaction_rewrite | after_rewrite_turn_12 | measured | 972 | 896 | 76 | 92.2% | none |
| compaction_rewrite | after_rewrite_turn_13 | measured | 1019 | 896 | 123 | 87.9% | none |
| compaction_rewrite | after_rewrite_turn_14 | measured | 1066 | 896 | 170 | 84.1% | none |
| compaction_rewrite | after_rewrite_turn_15 | measured | 1113 | 1024 | 89 | 92.0% | none |
| compaction_rewrite | after_rewrite_turn_16 | measured | 1160 | 1024 | 136 | 88.3% | none |
| compaction_rewrite | after_rewrite_turn_17 | measured | 1207 | 1152 | 55 | 95.4% | none |
| compaction_rewrite | after_rewrite_turn_18 | measured | 1254 | 1152 | 102 | 91.9% | none |
| compaction_rewrite | after_rewrite_turn_19 | measured | 1301 | 1152 | 149 | 88.5% | none |
| compaction_rewrite | after_rewrite_turn_20 | measured | 1348 | 1280 | 68 | 95.0% | none |
| compaction_rewrite | after_rewrite_turn_21 | measured | 1395 | 1280 | 115 | 91.8% | none |
| compaction_rewrite | after_rewrite_turn_22 | measured | 1443 | 1280 | 163 | 88.7% | none |
| compaction_rewrite | after_rewrite_turn_23 | measured | 1491 | 1408 | 83 | 94.4% | none |
| compaction_rewrite | after_rewrite_turn_24 | measured | 1539 | 1408 | 131 | 91.5% | none |
