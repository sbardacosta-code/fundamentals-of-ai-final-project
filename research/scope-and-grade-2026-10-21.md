# Would processing more than 100,000 reviews raise the grade? (2026-10-21)

Assistant analysis from saved evidence; the grading effect itself is the instructor's decision.

## What the grading documents say

- The instructor's latest announcement (2026-10-11, `research/instructor-update-2026-10-11.json`) sets the target as **100,000-review runs for no more than $10**, using caching, batching, parallelization and queues, combining Jev/GPT with a lightweight model such as Luna or Haiku. That target is now met by measurement: $4.74 Jev + $0.0042 Haiku for the full 100,000 sample.
- `COST_CALCULATOR.md` states there is **no reward for spending more or using more providers**.
- The older `GRADING_CONTRACT.md` and the checker's coverage formula still name the full corpus (660,622 rows):
  `0.2 × full-profile match + 0.2 × accounted rows / expected + 0.6 × valid classified / labelable`.
  The checker computes "expected" from whichever `--analysis` file the grader supplies.

## Measured coverage today

| Grader's `--analysis` file | Working coverage candidate |
| --- | ---: |
| `data/scale/sample_100000.csv` (declared scope) | 1.0 (99,994 of 100,000 valid; 6 quarantined) |
| `spotify_reviews_18months.csv` (full corpus) | about 0.32 (0.2 + 0.2 × 100,000/660,622 + 0.6 × 99,994/660,609) |

So more reviews only matter if the instructor grades against the full corpus despite the 100,000 announcement.

## Cost and time to finish the full corpus (modeled)

Using unit costs measured in the 100,000 run (Jev $0.0000479 per new unique text for enrichment, $0.0000493 per verified unique text, $0.0000145 per unique complaint text for grouping):

| Work beyond the sample | Count |
| --- | ---: |
| Remaining nonempty rows | 560,609 |
| New unique texts to enrich | 406,090 |
| New verified unique texts (same 10% rule) | 44,413 |
| New unique complaint texts (51.3% observed) | ~208,000 |

| Scenario | Jev cost | Wall time at the measured ~14 requests/s |
| --- | ---: | ---: |
| Base | ~$24.7 | ~2.6 hours |
| Conservative (+25%) | ~$30.8 | ~3.3 hours |

All of it is charged to **TypeSafe** credits; Anthropic credits cannot pay for Jev. Measured Jev usage so far totals about $4.78 (pilot, optimization trials and scale runs). If the original TypeSafe deposit was $5, roughly $0.22 remains, so a full run would first need about $25–31 more TypeSafe credit. The balance must be confirmed in the TypeSafe console.

## Recommendation

Stop at 100,000. It meets the instructor's stated target, the checker passes for the declared scope apart from 6 visible quarantines, and the materials say extra spending is not rewarded. Extending to the full corpus is only worth it if the instructor confirms that grading will use the full 660,622-row denominator; in that case it is a ~$25–31 TypeSafe top-up and a ~3-hour run with the same code (`python3 -m review_pipeline.scale --input data/source/spotify_reviews_18months.csv --name full_corpus ...`). The remaining grade leverage is higher in evaluation (the 50 human golden labels) and the deployed dashboard.
