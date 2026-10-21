# Spotify reviews: Jev multi-agent pipeline

## Scale checkpoints — October 20, 2026

Measured, with explicit student approval for each checkpoint and cap (separate ledgers in `artifacts/scale/scale.sqlite`; the $5 pilot ledger is untouched):

| Checkpoint | Rows | Unique texts | Workers | Jev cost | Cap | Verifier agreement (same model, not accuracy) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [checkpoint_500](cost/scale/checkpoint_500/run.json) | 500 | 479 | 2 | $0.029115 | $0.25 | 47/52 |
| [analysis_10000](cost/scale/analysis_10000/run.json) | 10,000 | 8,448 (500 reused from A) | 4 | $0.481008 | $1.50 | 864/986 |
| [sample_100000](cost/scale/sample_100000/run.json) | 100,000 | 78,099 (8,448 reused from A+B) | 8 | $4.230662 | $6.00 | 8,739/9,996 |

Recommendation memos use **Claude Haiku 5.5** (`claude-haiku-5-5`, Anthropic API credits) as the separate lightweight generative model: checkpoint_500 needed one retry after the validator rejected an invented claim ID ($0.002135 total); analysis_10000 passed first time ($0.000999). Jev remains the only classifier. The 500 checkpoint includes a saved interruption and resume (`checkpoint_before.json` 104 → `checkpoint_after.json` 500).

**100,000-review run (measured, completed 2026-10-21 ~05:37 UTC, i.e. after the Oct 20 Pacific midnight deadline):** deterministic sample `data/scale/scale_plan.json` (built by `python3 -m review_pipeline.scale_plan`), 99,994 completed and 6 quarantined after two invalid Jev outputs (visible, not hidden). 21,900 rows reused exact-text cache results. 22,605 successful requests plus 63 billed invalid outputs (each retried once), zero rate limits, 8 workers, ~26.5 minutes. Total Jev spend for the full 100,000 sample across A+B+run: **$4.740785** (0.029115 + 0.481008 + 4.230662), plus $0.004220 of Haiku memos. Modeled before the run: $4.21 for the run itself. This is below the instructor's $10 target by measured rate-calculated usage; it is not a provider invoice. The generated memo still needs human review (it characterizes the general-dissatisfaction issue as "update-related", which is not established by the metrics). Human golden labels, the `grading/` export, the dashboard and the recording remain pending. See [status report](research/status-2026-10-19.md) and [checkpoint plan](research/checkpoint-plan-500-10000.md). Raw call logs are gzip-compressed (`calls.jsonl.gz`).

## Latest instructor update — October 11, 2026

The student pasted Pepe’s new announcement, preserved verbatim in `research/instructor-update-2026-10-11.json`. It extends the deadline to “oct 20th midnight” and explicitly discusses **100,000-review runs costing no more than $10**, using caching, batching, parallelization, queues, and Jev/GPT decisions combined with a lightweight generative model such as Luna or Haiku. This supersedes the older full-corpus planning target below: plan a reproducible 100,000-review run, retaining full-file profiling and truthful scope reporting. The legacy checker still uses full-corpus denominators; disclose that mismatch rather than rewriting the instructor’s supplied files or claiming full-corpus completion. Exact deadline timezone/boundary is not specified in the pasted text.

Jev remains central. The current memo model is a separate lightweight generative model; Luna/Haiku are candidates for a measured comparison, not silently substituted into earlier evidence. The $10 is an instructor target, not a new authorization to charge the user: existing pilot accounting and authorization remain unchanged.

A preliminary projection from the saved baseline is approximately **$6.25 base / $7.81 with a 25% variable-cost buffer for 100,000 reviews**, even without exact-text savings. It assumes the pilot’s token/complaint mix, 10%verification and one memo. It is neither measured 100k spending nor a guaranteed upper bound. Freeze a deterministic representative 100k sample, measure its duplicate count and refresh the calculator after 500/10,000-review checkpoints before scale-up. Human golden labels remain independent and must not be used to tune prompts.

Earlier estimates and requirements below are historical unless compatible with this update.

The foundation preserves the Spotify source data, queues unique texts for Jev, validates typed decisions and records explicit task/cost states. It includes a blind human-labeling interface, evaluation, deterministic ranking and offline usage replay.

**Current status:** the full source file is ingested and profiled. The real 100-review Jev pilot completed enrichment, blind verification, grouping and ranking. Its warm replay made zero additional Jev calls. The OpenAI memo stage is implemented; its execution status and any missing evidence appear in [the pilot report](artifacts/pilot/report.md). The full dataset has not been classified or deployed; this is not yet the completed assignment.

## Architecture

CSV → source database → durable queue → parallel Jev enrichment workers → blind Jev verifier → Jev grouping → deterministic ranking → generative recommendation agent → database/backend/dashboard.

The durable queue, exact-text cache, shared spending ledger and Jev enrichment/verification/grouping roles are implemented. A deterministic orchestrator controls the dependent stages; the pilot deliberately uses one worker. Parallel queue claims are tested, but paid parallel speedups have not been measured. OpenAI receives a bounded aggregate evidence pack for the recommendation memo. Jev remains the primary decision engine. Full-scale coordination, the final grading export and the deployed product remain. See [implementation plan](research/jev-workflow-plan.md), [requirements](research/requirements.md), and the supplied [grading contract](data/source/GRADING_CONTRACT.md).

## Run locally

Python 3.9+; the foundation uses only the standard library. Run from this directory; no install is needed.

```sh
python3 -m review_pipeline --help
python3 -m unittest discover -s tests -v
python3 -m review_pipeline prepare --input data/source/spotify_reviews_18months.csv
python3 -m review_pipeline status
python3 -m review_pipeline label
```

The last command serves the human-labeling interface at http://127.0.0.1:8765. Labels start blank. Save each review and return later to resume. It writes `data/human/golden_labels.jsonl` atomically, checks stale revisions and never shows model predictions. The student must supply human answers; do not auto-label these records or use their answers for tuning. The interface binds only to localhost and validates Host/Origin.

Download the [instructor's dataset ZIP](https://drive.google.com/file/d/1P0rUoAS_wVjp3BYKqXMEyD4u0uJP1Bvf/view) into `data/source/`. The complete ZIP has eleven files; the preview can omit the largest CSV. Full CSV SHA-256: `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6`.

For an isolated cold pilot, use a **separate database**, e.g. `--db artifacts/pilot.sqlite --output artifacts/pilot-prepared.json --input data/source/cost_100.csv`. Never mix its cache with the full corpus. Cross-dataset exports reject cache provenance whose original review is outside the selected dataset.

`prepare` reports 660,622 original records, 13 empty texts, 159,701 missing versions, and 484,189 distinct nonempty texts. The queue has one job per unique text under the exact configuration. The remaining 176,420 nonempty rows can reuse exact results; every original ID still receives its own output row and business count. Empty texts are represented as quarantines during export. Preparation itself performs no classification.

## Offline evidence commands

```sh
python3 -m review_pipeline evaluate --golden data/human/golden_labels.jsonl --predictions artifacts/predictions.jsonl --output artifacts/evaluation.json
python3 -m review_pipeline rank --records artifacts/records.jsonl --membership artifacts/membership.csv --output artifacts/ranking.csv
python3 -m review_pipeline cost-replay --calls artifacts/calls.jsonl --rates config/rates.csv --output artifacts/cost-report.json
```

These require actual saved inputs. `config/rates.csv` contains dated standard rates for Jev and the pinned memo model. The calculator reports unknown usage/rates explicitly and withholds full-run projections until every pilot stage is complete. Active execution time and pauses are distinguished. Jev does not report a cached-token breakdown: its saved zero-discount policy bills every reported input token at the same published rate, retains raw usage, and makes no claim of measured provider cache savings.

`export --dataset SHA --config CONFIG_HASH --output artifacts/export` exports current records, calls and a checkpoint. Pending/uncertain tasks remain visibly incomplete. This is a state export, not a final grading folder. Read identifiers from `artifacts/prepared.json`.

## Jev and spending setup

The first 100-review pilot has a user-approved **$5 combined API ceiling**, recorded in `config/pilot.json`. The full run is not authorized for paid execution. Live commands require an explicit `--execute-paid` flag. Restarts reuse the existing spending ledger; do not create another pilot directory to reset the cap.

1. Complete account setup at the official https://console.typesafe.ai in the browser.
2. For a new setup, copy `.env.example` to `.env` and enter `TYPESAFE_API_KEY` and `OPENAI_API_KEY` locally. Do not overwrite an existing `.env`. Never paste keys in chat or put them in a browser bundle. Live commands safely load only the needed key.
3. The supporting memo model is `gpt-4.1-mini-2025-04-14`, capped at 1,500 output tokens. Provider credit deposits are separate from usage charges. The API usage budget does not authorize a subscription or hosting purchase.

```sh
# Explicit paid execution/resume, sharing one persistent $5 ledger:
python3 -m review_pipeline.pilot --execute-paid
python3 -m review_pipeline.pilot_memo artifacts/pilot --execute-paid
# Warm replay refuses new provider calls:
python3 -m review_pipeline.pilot --phase warm
python3 -m review_pipeline.pilot_memo artifacts/pilot --phase warm
# Offline report; no key, network request or paid call:
python3 -m review_pipeline.pilot_report artifacts/pilot
```

`python3 -m review_pipeline.workflow --phase cold --execute-paid` coordinates all six stages in one invocation. Once complete, `python3 -m review_pipeline.workflow` measures a fresh, fully cached warm run and preserves earlier warm measurements in `artifacts/pilot/history/`.

The Jev measurement includes one billed validation failure caused by independently rounded score/probability fields. Its raw response and cost remain in the log; `config_migrations.jsonl` documents the parser repair before any completed records. The saved checkpoints show 5 completed reviews before interruption and 100 after resume. A screen recording is still required for the assignment. The blind verifier disagreed on one of ten reviews; agreement from the same model is not a human accuracy score. `review_queue.csv` lists ambiguous/disputed reviews. Human golden labels remain a separate required task.

The completed pilot consumed **$0.010991572** from reported usage and dated rates, including the first memo's citation-validation failure and its successful retry. The complete warm replay made zero API calls. The generated memo passed numeric/ID checks but contained a false superlative; the separate [reviewed draft](cost/memo_reviewed.md) corrects that and an unsupported user-segment inference while preserving the original output. Human review is still pending. The initial full-run estimate is approximately $31.10–$38.87 with exact-text caching and one worker; this is an estimate, not authorization to scale.

Security controls: real credentials, source CSVs, local databases, private labels and raw research cache are excluded by `.gitignore`. These exclusions are not proof of clean Git history; inspect staged files before eventual publication. Golden labels and saved analysis evidence required for grading will be published deliberately in the final artifact stage.

## Persistence and limits

- SQLite WAL plus immediate transactions serialize claims and writes across worker connections; no shared JSON file is used as the work queue.
- Config hashes include role, model, versioned schema/prompt and adapter content. Independent verification has a separate cache namespace and no first-pass predictions in its request.
- Claims expire only before dispatch. In-flight calls with unknown outcomes are not automatically retried. Unknown charges hold their reservation.
- Each dispatch reserves cost against one database spending ledger; known final costs reconcile that reservation. Actual provider billing can differ, so this is an accounting control, not a guarantee against delayed bills.
- All-or-nothing prediction validation preserves original IDs and exact quote membership. Unsupported entities are rejected. Requests are capped at 50 records; initial pilot plan is five per request and one worker.
- Invalid attempts can retry once; queued retry state is bounded. Future live transient backoff and provider request/token throttling must be implemented before scale-up.

Tests use synthetic data and simulated provider responses, clearly separate from assignment execution evidence. They check atomicity, concurrent claims, cache provenance, schema failures, uncertainty accounting, arithmetic, human-save behavior and evaluation denominators.

## Cost optimization experiment evidence

The [experiment journal](cost/README.md#experiment-journal--cost-and-quality-2026-10-10) records student decisions, eight live prompt/batch trials, the frozen finalist’s held-out failure, cache tests, and accounting corrections. The cheaper finalist reduced classification cost by 43.1% but worsened held-out assistant-audit agreement, so it was rejected and the original classifier remains selected. No human-quality equivalence or reliable $20 production ceiling has been demonstrated. All trials plus the original pilot consumed $0.041233294 of the shared $5 usage budget; 86 automated tests pass. Raw evidence and the decision are under `cost/optimization/`.
