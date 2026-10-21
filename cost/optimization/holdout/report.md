# Provisional optimization audit

Assistant labels on 100 evaluation examples, not human golden labels. Human quality remains unknown; no automatic promotion.

| Candidate | Accepted topic | Accepted intent | Accepted severity | Accepted joint | Severity MAE | Severe recall | Provisional gate | Estimated full API USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| baseline | 0.95 | 1.0 | 0.98 | 0.92 | 0.12 | 0.8571428571428571 | reference | 29.90 |
| calibrated-b10 | 0.93 | 0.99 | 0.93 | 0.83 | 0.16 | 0.7142857142857143 | False | 19.97 |

Per-field matched/wrong IDs, primary metrics, missing counts, cost assumptions and metadata are in report.json.

- Assistant labels on 100 evaluation examples are provisional; human quality remains unknown.
- Baseline agreement and sentiment similarity measure consistency, not correctness.
- Accepted per-field scores permit any accepted value; joint score requires one complete accepted triple.
- Severe recall uses primary reference severity >=4, with missing predictions as misses.
- No automatic promotion. Costs are extrapolations; verification/grouping configuration is unchanged.
