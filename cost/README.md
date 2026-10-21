# Measured 100-review pilot

Read [report.md](report.md) for current completion status. These files contain real saved Jev outputs and usage for the unchanged instructor `cost_100.csv`, including a billed failed attempt. Missing stages remain explicit. The pilot uses five reviews per request and one worker, with a ten-review blind verification sample.

From the repository root, recompute the report without credentials or network calls:

```sh
python3 -m review_pipeline.pilot_report cost
```

Edit `rates.csv` to test pricing assumptions. `pilot_calls.jsonl` includes token units, role, model, request inputs, raw responses, timestamps, attempt outcome, and rate-calculated cost. Amounts are computed from reported usage and published rates, not an invoice reconciliation. Provider prompt-cache savings are not claimed for Jev: its API does not report that breakdown, and all input tokens receive the same rate. Local compute costs are unmeasured.

Cold measurements include a parser repair and an intentional saved interruption; active time excludes pauses. Checkpoints show growth from five to one hundred completed reviews, with no repeat successful enrichment for the first five. The assignment's screen recording remains outstanding. All ten verifier requests omit the first-pass labels. Same-model agreement does not establish accuracy; human evaluation and review of flagged cases remain required.

The live commands below use the original durable database in `artifacts/pilot/`, sharing a combined $5 cap across Jev and OpenAI. They require valid credentials and any necessary provider credit. Do not create a fresh directory to bypass that ledger.

```sh
python3 -m review_pipeline.pilot --execute-paid
python3 -m review_pipeline.pilot_memo artifacts/pilot --execute-paid
python3 -m review_pipeline.pilot --phase warm
python3 -m review_pipeline.pilot_memo artifacts/pilot --phase warm
python3 -m review_pipeline.pilot_report artifacts/pilot
```

For the complete saved program, use `python3 -m review_pipeline.workflow --phase cold --execute-paid`; the default `python3 -m review_pipeline.workflow` performs only a fully cached warm run and measures its end-to-end wall time.

After a new measurement, refresh the copied evidence in this folder. The full 660,622-row run is not authorized or executed. Projections only become available when the required measured stages are complete; they are estimates, not promises or authorization to scale. The eventual full run must account for 660,609 nonempty rows and 13 empty-text quarantines. Further 500/10,000-review experiments require an agreed budget.

## Experiment journal — cost and quality, 2026-10-10

This is the project’s grading evidence trail. It separates student decisions, automated execution, and assistant judgments. Failed experiments are retained. Reported API costs come from usage and dated rates, not a provider invoice.

### Student decisions and requirements

The student requested queues, caching, parallel agents with an explicit workflow design, and batching. The student corrected the initial direction to make **Jev central**, citing the professor’s repeated instructions; that correction remains part of the project history. The student authorized a **$5 combined pilot usage ceiling**, set up Jev and OpenAI accounts, and reported depositing $5 into each. Deposits are prepaid balances, not measured consumption. After questioning why the projection exceeded the professor’s reported approximately $20 expectation, the student authorized cheaper trials **without sacrificing quality** and explicitly requested preserving discoveries and tests for grading.

These are student decisions; the runs and audits below were performed by the assistant’s tools and agents. The student has not been represented as personally executing these commands or supplying the assistant reference labels.

### Workflow and controls

An orchestrator coordinates ingestion, enrichment, independent verification, grouping, ranking, and memo generation. SQLite provides the durable queue, leases, exact-result cache, and shared spend reservations. Cache identities include model/prompt/schema/adapter configuration. Batches of 5, 10, and 20 were investigated. Parallel assistant agents handled the experimental adapter, blind reference labeling, report arithmetic/tests, and an independent accounting/split audit. This is an orchestrated workflow, not an unbounded swarm. Paid Jev trial requests used one worker so concurrency would not confound comparisons. Claim-concurrency tests use simulated responses, not measured live throughput.

All trials reused the original global $5 ledger; an additional $0.25 optimization limit was enforced. No new ledger reset the budget. Unknown-cost outcomes block further dispatch. The full dataset has not been run.

### Original pilot discoveries

- The 100-review pilot completed all six stages. Its cost was **$0.010991572**, including two billed failed attempts across Jev and the memo model.
- Jev independently rounds probability fields; strict arithmetic validation initially rejected a billed response. The parser was repaired with bounded rounding tolerance, and the failed attempt remains logged in [pilot_calls.jsonl](pilot_calls.jsonl) and [config_migrations.jsonl](config_migrations.jsonl).
- The memo’s first attempt failed citation validation. The successful generated memo still contained a false superlative and an unsupported segment inference. The [reviewed draft](memo_reviewed.md) corrects these while preserving the original. Numeric checks alone do not establish sound interpretation.
- Resume checkpoints progressed from 5 to 100 reviews without repeating already successful enrichment. The warm workflow used zero API calls. A screen recording remains outstanding.
- Exact-text reuse reduces 660,609 nonempty rows to 484,189 distinct classification texts, preserving every original ID and count. The initial projected total was **$31.10 base / $38.87 conservative**, including observed retry costs. It was an extrapolation, not actual spending.

### Optimization hypotheses and trials

The [development comparison](optimization/development/report.md) contains all eight tested settings. Each used the same 100 development reviews and five output questions.

| Trial | Hypothesis | Measured enrichment cost for 100 rows | Development decision |
| --- | --- | ---: | --- |
| compact-b5 | Shorten option descriptions and common instructions | $0.004044558 | Failed quality gate |
| shared-b5 | Put shared rubric in request state once | $0.003201198 | Failed quality gate |
| shared_short-b5 | Shorten references to the shared rubric | $0.002730798 | Failed quality gate |
| shared_short-b10 | Amortize shared state across 10 reviews | $0.002307438 | Failed severity gate |
| shared_short-b20 | Further amortize state across 20 reviews | $0.002106258 | Rejected; intent agreement and severe detection declined |
| shared_rules-b10 | Restore concise explicit precedence rules | $0.002643438 | Failed quality gate |
| hybrid-b10 | Keep original severity question, shorten other questions | $0.003105858 | Failed quality gate |
| calibrated-b10 | Clarify functional impact versus emotional wording | $0.002714838 | Passed development gate; selected for held-out check |

The provisional gate required no decline versus baseline in primary and accepted topic/intent/severity agreement, severity absolute error, or severe-case recall, with all 100 outputs present. Reference labels were made by an independent assistant auditor without seeing predictions or ratings. They include alternate acceptable labels for ambiguous cases; they are **not human golden labels or established accuracy**. Human golden data was not used for tuning.

### Held-out check and rejected finalist

The final candidate and adapter hash were frozen in [selection.json](optimization/holdout/selection.json) before its held-out inference. The separate 100-row set came from checkpoint rows 101–200. Baseline and candidate were both run cold. No prompt was tuned after examining these results.

| Held-out measure | Original baseline, batch 5 | Frozen candidate, batch 10 |
| --- | ---: | ---: |
| Enrichment cost, 100 rows / 99 unique texts | $0.004709292 | $0.002678046 |
| Accepted complete-label agreement | 92/100 | 83/100 |
| Accepted topic agreement | 95/100 | 93/100 |
| Accepted intent agreement | 100/100 | 99/100 |
| Accepted severity agreement | 98/100 | 93/100 |
| Primary topic / intent / severity agreement | 88 / 96 / 88 of 100 | 84 / 95 / 84 of 100 |
| Mean absolute severity error | 0.12 | 0.16 |
| Severe cases detected | 6/7 | 5/7 |

The candidate reduced measured classification cost by **43.1%**, but failed the quality gate. **It was rejected for production; the original classifier remains selected.** Its full-run projection is approximately $19.97 base / $24.96 conservative, with verification/grouping unchanged. Therefore even this rejected candidate did not establish a reliable $20 ceiling.

The independent split audit found no repeated IDs, but three repeated short texts across development and holdout, covering four held-out rows. Removing those four rows still gives baseline **88/96** versus candidate **79/96** accepted joint agreement, supporting the same rejection. The sample contains only seven severe cases; it does not establish statistical noninferiority. See [split_audit.json](optimization/split_audit.json) and the [full held-out report](optimization/holdout/report.md).

### Verification, accounting, and evidence

- **86 automated tests passed.** They cover queue/cache behavior, validation, bounded retries, budget reconciliation, unknown-charge holds, and reporting arithmetic. Synthetic tests are separate from live trial evidence.
- An offline replay with a client that refuses provider calls recovered all 100 candidate rows with **zero new API calls** in about 0.056 seconds. Cold measurements were preserved separately.
- Optimization trials cost **$0.030241722**. The original pilot plus all trials total **$0.041233294** (about 4.1 cents), with $0 reserved and no unknown charges. All trial counts and costs reconcile to SQLite: 171 total calls, 169 successful and two failed.
- [decision.json](optimization/decision.json) records rejection and spend. Development and holdout directories retain predictions, references, metadata, raw requests/responses, usage, and report outputs. [evidence_sha256.json](optimization/evidence_sha256.json) fingerprints these files.
- Packaged `development/calls.jsonl` and `holdout/calls.jsonl` are filtered to disjoint configuration IDs. The original working `artifacts/optimization*/calls.jsonl` files are cumulative overlapping snapshots: **do not add their totals together**.
- Held-out cost extrapolation uses **99 distinct enrichment texts**, not 100 rows; that denominator was corrected and regression-tested. No provider prompt-cache discount is claimed.

### Current conclusion and remaining work

The trials found cheaper prompts, but did **not** establish cheaper production behavior with preserved quality. Exact-result caching and restart reuse remain the demonstrated ways to avoid repeated work without changing saved predictions. Human golden labeling, evaluation against those labels, final memo review, and the required recording remain outstanding. Keep the original classifier while considering further separately evaluated cost changes; do not claim the failed candidate or a sub-$20 production result as an achievement.

## Follow-up: duplicate reuse and verification scope

The student explicitly raised the professor’s duplicate-review example. Exact-text reuse was already implemented and included in the quoted estimate: 660,609 nonempty rows minus 484,189 distinct texts gives **176,420 reusable duplicate rows**. The saved base projection falls from $41.25298858948 without reuse to $31.10037395788 with reuse, approximately $10.15 saved. Every original review still contributes to business counts. Similar wording is not treated as an exact duplicate. The class-note summary records caching guidance; these exact counts come from the dataset profile, not an asserted transcript quote.

A fresh read of the Google Docs assignment confirms that verification requires a declared random blind sample without a numeric fraction. Ten of 100 is our pilot choice; extrapolating it to 10% of the corpus was a modeling assumption. [verification_scenarios.json](verification_scenarios.json) preserves alternative estimates with the original classifier unchanged: 1,000 verifications projects about $27.92 base / $34.90 buffered. This is not an adopted configuration or measured full run; smaller samples reduce audit precision.

The current brief still says “or at least 100,000” in its submission checklist while its coverage formula and dataset contract use full-corpus denominators. Full scope remains the planning basis until the grading effect of reduced scope is clarified. No message was sent to the instructor.

The human labeling interface was opened in Chrome and showed 0/50 saved. The first review was visible during page verification; no answer was supplied or saved by the assistant, and it was not used for prompt tuning. The student should complete labels independently before evaluation.

## Latest instructor update — October 11, 2026

The student pasted Pepe’s new announcement, preserved verbatim in `research/instructor-update-2026-10-11.json`. It extends the deadline to “oct 20th midnight” and explicitly discusses **100,000-review runs costing no more than $10**, using caching, batching, parallelization, queues, and Jev/GPT decisions combined with a lightweight generative model such as Luna or Haiku. This supersedes the older full-corpus planning target below: plan a reproducible 100,000-review run, retaining full-file profiling and truthful scope reporting. The legacy checker still uses full-corpus denominators; disclose that mismatch rather than rewriting the instructor’s supplied files or claiming full-corpus completion. Exact deadline timezone/boundary is not specified in the pasted text.

Jev remains central. The current memo model is a separate lightweight generative model; Luna/Haiku are candidates for a measured comparison, not silently substituted into earlier evidence. The $10 is an instructor target, not a new authorization to charge the user: existing pilot accounting and authorization remain unchanged.

A preliminary projection from the saved baseline is approximately **$6.25 base / $7.81 with a 25% variable-cost buffer for 100,000 reviews**, even without exact-text savings. It assumes the pilot’s token/complaint mix, 10%verification and one memo. It is neither measured 100k spending nor a guaranteed upper bound. Freeze a deterministic representative 100k sample, measure its duplicate count and refresh the calculator after 500/10,000-review checkpoints before scale-up. Human golden labels remain independent and must not be used to tune prompts.

Earlier estimates and requirements below are historical unless compatible with this update.

## Follow-up 2026-10-19: deterministic 100,000 sample and checkpoint design

No paid calls. The 100,000 scope now has a frozen input: the instructor's own seeded order extended to 100,000 rows (`data/scale/sample_100000.csv`, SHA-256 `cf2641e8f3075e392a4c693c7cd45484c68e907569c48718cf131f0e321d643c`). Golden 50, cost 100, checkpoint 500 and analysis 10,000 were verified as exact nested prefixes. Exact duplicates measured inside the sample: 78,099 unique texts of 100,000 (21.9% reusable), fewer duplicates than the full-corpus ratio implies (73,295). Modeled cost from measured pilot unit costs: **$4.91 base / $6.14 conservative**, replacing the earlier no-reuse $6.25 / $7.81 figure. This is a projection, not spending. See [scale_plan.json](scale_plan.json), [status report](../research/status-2026-10-19.md) and [checkpoint plan](../research/checkpoint-plan-500-10000.md). A new parallel checkpoint runner (`review_pipeline/scale.py`) is tested only with synthetic providers; 99 tests pass.
