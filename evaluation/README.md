# Evaluation against the student's 50 human golden labels

The student labeled `data/source/golden_50_to_label.csv` in the local labeling tool without seeing any model prediction (2026-10-21). The labels were never used in prompts, examples, thresholds or tuning; they were read only after the 100,000-review run was finished. Predictions are the unchanged first-pass Jev records from that run (`golden_predictions.jsonl`). Computed offline:

```sh
python3 -m review_pipeline evaluate --golden evaluation/golden_labels.jsonl \
  --predictions evaluation/golden_predictions.jsonl --output evaluation/golden_evaluation.json
```

## Results (measured, n = 50, no missing or invalid predictions)

| Metric | Value |
| --- | ---: |
| Topic exact agreement | 42/50 (84%) |
| Intent exact agreement | 43/50 (86%) |
| Severity exact agreement | 34/50 (68%) |
| Severity mean absolute error | 0.42 |
| Topic, intent and severity all exact | 23/50 (46%) |
| Sentiment MAE (−1…1 scale) | 0.243; 66% within ±0.2 |
| Exact evidence quotes | 50/50 (full review text is the quote; support still needs human judgment) |
| Entities exact | 96% |
| `needs_review` matches human ambiguity | 80% (human marked 6 reviews ambiguous) |

For comparison, the assistant-made blind reference labels on the held-out optimization set gave 88% topic, 96% intent and 88% severity primary agreement. The human set is smaller and stricter, so these numbers are the more credible quality estimate; 50 reviews still give wide uncertainty (roughly ±10 points on each agreement rate).

## Error analysis

- **Severity is the weakest field and is biased upward.** In 16 disagreements the model was higher than the human 14 times (12 by one level). The model tends to read annoyance as degraded function (2 → 3). One large miss goes the other way: a review about ads suddenly playing at loud volume, which the human rated 5 (safety harm) and the model rated 2.
- **Topic disagreements are mostly `other` vs a specific feature on positive reviews.** The human used `other` for general praise in cases where the model picked the praised feature (catalog for recommendations, usability for "less advertising"). The contract says a positive review should take the first specific praised feature, so several of these are rubric boundary cases rather than clear model errors; the human labels were kept exactly as given.
- **Intent errors sit on the unclear / complaint / praise boundary** for very short texts ("Great...", one-word or non-English texts) and on a boycott slogan, which is the same boundary where the blind verifier disagreed most in the 100,000 run ([verifier analysis](../research/verification-analysis-100k.md)).
- **The ambiguity flag is a weak triage signal here:** 8 of the 14 flagged reviews had at least one label disagreement, but 19 of the 36 unflagged reviews did too.

## What this means

Topic and intent are reliable enough for aggregate issue ranking; severity, and therefore priority scores, should be read as approximate and somewhat inflated. The most useful next improvement would be a severity rubric clarification on "annoyance vs degraded function", evaluated on fresh data rather than on these 50 labels, which must stay untouched by tuning.
