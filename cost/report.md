# Offline pilot cost and runtime report

Complete pilot evidence: True

## Saved measurements

| Run | Attempts | Known API cost (USD) | Unknown-cost attempts | Measured seconds | Runtime basis | Complete |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| cold | 36 | 0.010991572 | 0 | 17.042553874 | active_attempts | True |
| warm | 0 | 0 | 0 | 0.043230791000000005 | single_workflow_wall_clock | True |

Known subtotals exclude unresolved costs. Local compute costs are unknown.

- cold measured wall window: 413.215 seconds; paused/between-command time: 396.173 seconds.
- warm measured wall window: 0.043 seconds; paused/between-command time: 0.000 seconds.

## Estimated scenarios

- base_exact_reuse: $31.10 API; estimated active runtime: 7.37 hours; over budget: True.
- conservative_exact_reuse: $38.87 API; estimated active runtime: 9.20 hours; over budget: True.
- base_no_reuse: $41.25 API; estimated active runtime: 9.83 hours; over budget: True.
- conservative_no_reuse: $51.57 API; estimated active runtime: 12.28 hours; over budget: True.

## Evidence gaps


## Assumptions

- Projections are estimates from 100 reviews, not executed full-run results; refresh at 500 and 10000.
- Same model configuration, worker limit, review mix, verification fraction and observed retry/fallback mix are assumed.
- Exact-result reuse applies to enrichment/grouping; random verification remains over original IDs.
- Grouping scales with observed unique complaint share; memo cost occurs once and is not multiplied by review count.
- Conservative scenarios add 25% variable model cost/time for higher retries/fallbacks; this is an editable planning assumption in code, not measured overhead.
- Runtime requires nonoverlapping stage elapsed times; no linear parallel speedup is assumed.
- Active-attempt runtime excludes pauses between invocations; wall elapsed and pause duration are shown only if separately measured.
- Missing local compute costs remain unknown. Pilot completion does not certify source checksum, human labels or grading compliance.

Detailed stage costs, calls, rates, metadata and source links are in report.json.
