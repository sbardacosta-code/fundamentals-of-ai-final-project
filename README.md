# Spotify reviews: Jev multi-agent pipeline

Fundamentals of AI final project. A reproducible pipeline that turns 100,000 Spotify app reviews into a ranked list of product issues and a short recommendation memo, using **Jev** (TypeSafe) as the structured decision engine and **Claude Haiku 5.5** as a separate lightweight writer. Every number below links to saved evidence; nothing here requires an API key to inspect.

**Live dashboard:** https://fundamentals-of-ai-spotify-insights.vercel.app (public, no login). It reads a read-only Python backend (`dashboard/api/data.py`) over a SQLite database of the 100,000 processed reviews, issue ranking, checkpoint metrics and the Haiku memo (`dashboard/data/reviews.sqlite`, rebuilt with `python3 dashboard/build_db.py`). Deployed with `cd dashboard && vercel deploy --prod`. The dashboard shows saved results only; it never calls a model.

## What the project does, in plain terms

1. **Ingest.** Read the instructor's CSV (660,622 rows), hash every original row, and freeze a deterministic 100,000-review sample using the instructor's own seed and ordering (`python3 -m review_pipeline.scale_plan`).
2. **Queue and cache.** Put one task per *unique exact text* into a durable SQLite queue. 100,000 rows contain 78,099 unique texts, so 21,900 rows reuse a saved result instead of a new paid call. Every original review still keeps its own output row and counts separately.
3. **Enrich (agent 1, Jev).** Parallel workers claim batches of 5 reviews and ask Jev for topic, intent, severity, sentiment and ambiguity under the shared rubric. Code validates every returned ID and label before saving.
4. **Verify (agent 2, Jev, blind).** A declared 10% random sample is classified again from scratch without seeing the first answers; code compares the two.
5. **Group (agent 3, Jev).** Complaint and cancellation reviews are assigned to issues from a fixed catalog.
6. **Rank (code, no model).** Priority = sum of severity per issue, exactly as the grading contract specifies.
7. **Recommend (agent 4, Claude Haiku 5.5).** Haiku receives only the aggregated numbers and short cited excerpts, never the corpus. A validator rejects any invented number, claim ID, issue or review ID.

## Architecture: orchestration, not a swarm

A deterministic orchestrator written in Python (`review_pipeline/scale.py`) owns the control flow: stage order, spending caps, retries and stop conditions. No model decides what runs next. Inside each stage, N bounded workers (2, 4, then 8) pull batches from the same SQLite queue in parallel and share one spending ledger and one request-rate limiter. The "agents" are separate roles with their own instructions, inputs, outputs and saved evidence; they never call each other. A swarm was rejected because the work has fixed inputs, strict dependencies and a shared budget, so free-form agent coordination would add cost and unpredictability without benefit.

```
CSV ─► durable queue + exact-text cache (SQLite, spend ledger)
          │
          ├─► enrich: Jev × N workers (batch 5) ──► records
          │        ├─► verify: Jev blind 10% sample ──► agreement report
          │        └─► group: Jev on complaints ──► membership
          │                         └─► rank (code) ──► ranking.csv, claims.csv
          │                                      └─► memo: Claude Haiku 5.5 ──► memo.json
          └─ checkpoints, interruption/resume, cost and call logs at every step
```

The four required techniques: **queues** (SQLite jobs with leases, idempotent commits and resumable state), **caching** (exact text + full model/prompt/schema configuration as the key), **batching** (5 reviews per Jev request, at most 50 allowed), **parallelization** (bounded workers under a shared rate limit and cap reservation before every dispatch).

## How we scaled safely

| Step | Rows | Workers | Measured Jev cost | Approved cap |
| --- | ---: | ---: | ---: | ---: |
| Pilot | 100 | 1 | $0.010992 incl. memo | $5 (pilot ledger) |
| Checkpoint A | 500 | 2 | $0.029115 | $0.25 |
| Checkpoint B | 10,000 | 4 | $0.481008 | $1.50 |
| Final run | 100,000 | 8 | $4.230662 | $6.00 |

Each step was approved separately by the student with its projected cost and hard cap. The full 100,000 sample cost **$4.74 in Jev usage plus $0.0042 in Haiku memos**, under the instructor's $10 target (rate-calculated from reported usage, not a provider invoice). Failed prompt-compression experiments are kept in the [experiment journal](cost/README.md).

## Grading export and self-check

`grading/` follows the contract (`python3 -m review_pipeline.grading_export`). The supplied zero-API checker, run with the 100,000 sample as `--analysis`, reports 100,000 of 100,000 IDs accounted for, 99,994 valid completed, 6 quarantined after two invalid Jev outputs, 21,900 valid cache reuses, and a working coverage candidate of 1.0; its only flag is `unfinished_classification: 6`. See [cost/self-check-100k.json](cost/self-check-100k.json). Against the full 660,622-row corpus the same evidence would cover only about 15% of rows; see [grading evaluation](research/scope-and-grade-2026-10-21.md).

## Where to find everything

| What the brief asks for | Where it is |
| --- | --- |
| Runnable code and setup | `review_pipeline/` (Python 3.9+, standard library; `anthropic` package only for the Haiku memo), [Run locally](#run-locally) |
| Role prompts | Enrichment and verification rubric: [`review_pipeline/jev.py`](review_pipeline/jev.py) (`TOPICS`, `INTENTS`, `SEVERITIES`, `build_request`); grouping catalog and prompt: [`review_pipeline/grouping.py`](review_pipeline/grouping.py); memo prompt: [`review_pipeline/memo.py`](review_pipeline/memo.py) (`PROMPT`) with the Haiku request in [`review_pipeline/scale_memo.py`](review_pipeline/scale_memo.py) |
| Architecture | [Architecture](#architecture-orchestration-not-a-swarm) above; orchestrator [`review_pipeline/scale.py`](review_pipeline/scale.py) |
| Saved outputs | [`grading/`](grading) (contract export), [`cost/scale/`](cost/scale) (per-checkpoint records, calls, memos) |
| Evaluation | [Human golden-label evaluation](evaluation/README.md) (topic 84%, intent 86%, severity exact 68%, MAE 0.42), [verifier analysis](research/verification-analysis-100k.md), optimization quality gates in [`cost/README.md`](cost/README.md), synthetic robustness tests in [`tests/`](tests) |
| Cost evidence and calculator | Pilot calculator [`cost/report.md`](cost/report.md) (`python3 -m review_pipeline.pilot_report cost`, offline), [measured scale runs](cost/scale/README.md), [100k sample plan](cost/scale_plan.json) |
| Memo | [`grading/memo_reviewed.md`](grading/memo_reviewed.md) (human-reviewed and approved), original model output [`grading/memo.json`](grading/memo.json) |
| Dashboard, backend, database | https://fundamentals-of-ai-spotify-insights.vercel.app, [`dashboard/`](dashboard) |
| Interruption and resume | Screen recording [`evidence/interruption-resume-demo.mp4`](evidence/interruption-resume-demo.mp4) (50 fresh reviews, interrupted at 20, resumed to 50, 0 re-sent, $0.0029; script [`scripts/demo_resume.sh`](scripts/demo_resume.sh), evidence [`cost/demo_resume/`](cost/demo_resume)); graded run [`grading/checkpoint_before.json`](grading/checkpoint_before.json) (104 IDs) → [`grading/checkpoint_after.json`](grading/checkpoint_after.json) |
| Checker self-check | [`cost/self-check-100k.json`](cost/self-check-100k.json) |

## Current status (2026-10-21)

Done and measured: 100,000-review run, blind verification, grouping, ranking, Haiku memo (human-reviewed), grading export with checker self-check, deployed dashboard with backend, database and animated walkthrough, evaluation against the student's 50 human golden labels, recorded interruption/resume demo, 106 passing tests.

Pending: nothing required. Scope is the instructor's 100,000-review target, not the full 660,622-row corpus ([why](research/scope-and-grade-2026-10-21.md)).

## Reproduce the scaled run

```sh
python3 -m review_pipeline.scale_plan                       # offline: sample, duplicates, cost model
python3 -m review_pipeline.scale --input data/scale/sample_100000.csv --name sample_100000 \
  --expected-rows 100000 --budget-usd 6 --workers 8 --requests-per-second 16 --execute-paid   # paid, Jev
python3 -m review_pipeline.scale_memo sample_100000 --budget-usd 0.10 --execute-paid        # paid, Haiku
python3 -m review_pipeline.grading_export                    # offline
python3 data/source/check_submission.py reference --full data/source/spotify_reviews_18months.csv \
  --analysis data/scale/sample_100000.csv --out local-reference.json
python3 data/source/check_submission.py check --reference local-reference.json --submission grading --out self-check.json
```

Re-running a completed checkpoint makes no new calls: every task is already in the cache. Paid commands refuse to run without `--execute-paid`, and each ledger's cap is fixed when created.

## Project history (kept for the grading record)

Sections below describe the project at the time they were written. Statements such as "not yet deployed" or "pending" in them were true then and are superseded by the status above.

## Scale checkpoints — October 20, 2026

Measured, with explicit student approval for each checkpoint and cap (separate ledgers in `artifacts/scale/scale.sqlite`; the $5 pilot ledger is untouched):

| Checkpoint | Rows | Unique texts | Workers | Jev cost | Cap | Verifier agreement (same model, not accuracy) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [checkpoint_500](cost/scale/checkpoint_500/run.json) | 500 | 479 | 2 | $0.029115 | $0.25 | 47/52 |
| [analysis_10000](cost/scale/analysis_10000/run.json) | 10,000 | 8,448 (500 reused from A) | 4 | $0.481008 | $1.50 | 864/986 |
| [sample_100000](cost/scale/sample_100000/run.json) | 100,000 | 78,099 (8,448 reused from A+B) | 8 | $4.230662 | $6.00 | 8,739/9,996 |

Recommendation memos use **Claude Haiku 5.5** (`claude-haiku-5-5`, Anthropic API credits) as the separate lightweight generative model: checkpoint_500 needed one retry after the validator rejected an invented claim ID ($0.002135 total); analysis_10000 passed first time ($0.000999). Jev remains the only classifier. The 500 checkpoint includes a saved interruption and resume (`checkpoint_before.json` 104 → `checkpoint_after.json` 500).

**100,000-review run (measured, completed 2026-10-21 ~05:37 UTC, i.e. after the Oct 20 Pacific midnight deadline):** deterministic sample `data/scale/scale_plan.json` (built by `python3 -m review_pipeline.scale_plan`), 99,994 completed and 6 quarantined after two invalid Jev outputs (visible, not hidden). 21,900 rows reused exact-text cache results. 22,605 successful requests plus 63 billed invalid outputs (each retried once), zero rate limits, 8 workers, ~26.5 minutes. Total Jev spend for the full 100,000 sample across A+B+run: **$4.740785** (0.029115 + 0.481008 + 4.230662), plus $0.004220 of Haiku memos. Modeled before the run: $4.21 for the run itself. This is below the instructor's $10 target by measured rate-calculated usage; it is not a provider invoice. The generated memo still needs human review (it characterizes the general-dissatisfaction issue as "update-related", which is not established by the metrics). At that time the `grading/` export and dashboard were still pending; both are now done (see above). See [status report](research/status-2026-10-19.md) and [checkpoint plan](research/checkpoint-plan-500-10000.md). Raw call logs are gzip-compressed (`calls.jsonl.gz`).

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
