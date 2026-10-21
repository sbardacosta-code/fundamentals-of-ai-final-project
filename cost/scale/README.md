# Measured scale runs: 500, 10,000 and 100,000 reviews

Real paid runs, each approved by the student with its projected cost and hard cap before execution. Costs are recalculated from reported token usage and the dated rates in [`../rates.csv`](../rates.csv) (Jev $0.042 per million input tokens, outputs $0; Haiku 5.5 $0.10 / $0.50 per million input / output). They are not provider invoices. Nested checkpoints share one exact-text cache, so each row shows **incremental** spending.

| Run | Rows | Workers | Requests | Failed (billed, retried) | Enrich | Verify | Group | Memo (Haiku) | Total | Active seconds | Requests/s | Cost per 1,000 rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [checkpoint_500](checkpoint_500/run.json) | 500 | 2 | 157 | 1 | $0.023075 | $0.002560 | $0.003480 | $0.002135 | $0.031250 | 39 | 4.0 | $0.06250 |
| [analysis_10000](analysis_10000/run.json) | 10,000 | 4 | 2,566 | 7 | $0.381810 | $0.041809 | $0.057389 | $0.001000 | $0.482008 | 330 | 7.8 | $0.04820 |
| [sample_100000](sample_100000/run.json) | 100,000 | 8 | 22,668 | 63 | $3.338456 | $0.371561 | $0.520645 | $0.001086 | $4.231747 | 1,591 | 14.2 | $0.04232 |
| **Full 100,000 sample (sum)** | 100,000 | | 25,391 | 71 | | | | | **$4.745005** | | | **$0.04745** |

Notes:

- Requests per second were capped by the run's own limiter (4, 8, then 16 per second); no HTTP 429 rate limits were received in any run. Throughput grew with workers but not linearly.
- The memo for checkpoint_500 includes one billed invalid attempt (an invented claim ID rejected by the validator) and its retry.
- 6 of 100,000 reviews are quarantined after two invalid Jev outputs on the same batch; they are visible in `records.jsonl.gz` and the checker report.
- Cost per completed record for the full sample: $4.745005 / 99,994 = $0.0000475.
- Earlier projections for comparison: $4.91 base (from the pilot, before any checkpoint), $4.75 (refreshed after checkpoint A), $4.21 for the final run alone (refreshed after B; measured $4.23).

Projection for the remaining full corpus (not executed, not authorized): see [`../../research/scope-and-grade-2026-10-21.md`](../../research/scope-and-grade-2026-10-21.md).
