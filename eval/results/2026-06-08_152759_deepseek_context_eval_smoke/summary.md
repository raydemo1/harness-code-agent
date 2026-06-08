# DeepSeek Context Cache Eval

Created at: 2026-06-08T15:28:10

## Scenario Summary

| Scenario | Turns | Avg Hit Ratio | First -> Last | Prefix Changes |
| --- | ---: | ---: | ---: | --- |
| stable_warmup | 2 | 49.4% | 0.0% -> 98.8% | none |

## Calls

| Scenario | Label | Kind | Prompt | Hit | Miss | Hit Ratio | Prefix Change |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| stable_warmup | stable_warmup_turn_1 | measured | 3463 | 0 | 3463 | 0.0% | none |
| stable_warmup | stable_warmup_turn_2 | measured | 3498 | 3456 | 42 | 98.8% | none |
