# Provisional optimization audit

Assistant labels on 100 evaluation examples, not human golden labels. Human quality remains unknown; no automatic promotion.

| Candidate | Accepted topic | Accepted intent | Accepted severity | Accepted joint | Severity MAE | Severe recall | Provisional gate | Estimated full API USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| baseline | 0.91 | 0.98 | 0.96 | 0.86 | 0.11 | 1.0 | reference | 31.10 |
| calibrated-b10 | 0.95 | 0.99 | 0.97 | 0.91 | 0.09 | 1.0 | True | 20.01 |
| compact-b5 | 0.91 | 0.98 | 0.94 | 0.84 | 0.12 | 1.0 | False | 26.45 |
| hybrid-b10 | 0.96 | 0.99 | 0.93 | 0.89 | 0.13 | 1.0 | False | 21.91 |
| shared-b5 | 0.96 | 0.96 | 0.95 | 0.88 | 0.12 | 1.0 | False | 22.37 |
| shared_rules-b10 | 0.93 | 0.99 | 0.95 | 0.88 | 0.12 | 0.8 | False | 19.67 |
| shared_short-b10 | 0.95 | 0.98 | 0.94 | 0.89 | 0.12 | 1.0 | False | 18.04 |
| shared_short-b20 | 0.91 | 0.9 | 0.91 | 0.8 | 0.18 | 0.6 | False | 17.07 |
| shared_short-b5 | 0.93 | 0.96 | 0.93 | 0.85 | 0.13 | 1.0 | False | 20.09 |

Per-field matched/wrong IDs, primary metrics, missing counts, cost assumptions and metadata are in report.json.

- Assistant labels on 100 evaluation examples are provisional; human quality remains unknown.
- Baseline agreement and sentiment similarity measure consistency, not correctness.
- Accepted per-field scores permit any accepted value; joint score requires one complete accepted triple.
- Severe recall uses primary reference severity >=4, with missing predictions as misses.
- No automatic promotion. Costs are extrapolations; verification/grouping configuration is unchanged.
