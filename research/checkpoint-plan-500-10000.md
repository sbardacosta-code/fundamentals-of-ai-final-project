# Checkpoint plan: 500, then 10,000, before any 100,000 paid run

Status: **proposed, not approved, nothing executed.** All dollar figures are modeled from the measured pilot (`cost/scale_plan.json`). Paid execution requires Tito's explicit approval of each checkpoint, its projected cost and its hard cap.

## Fixed design (all checkpoints)

| Setting | Value | Why |
| --- | --- | --- |
| Classifier | Jev `jev-1.13.0`, original prompt `jev-rubric-v1`, unchanged adapter | Only configuration that passed held-out checks; cheaper variants were rejected |
| Batch size | 5 reviews per request (hard max 50) | The original classifier was only validated at batch 5; batch 10/20 were tested only with changed prompts |
| Store | `artifacts/scale/scale.sqlite`, separate from the pilot DB | Pilot ledger and evidence stay untouched; nested checkpoints share one exact-text cache |
| Ledger | One new ledger per checkpoint; hard cap fixed at creation, cannot change on resume | No silent cap reset |
| Reservation | $0.01 per in-flight request (observed ≈ $0.00024) | Spend + reserved + next reservation must stay under cap |
| Verification | Blind 10% sample: review verified if SHA-256(`scale-verify-v1:`+id)/2^256 < 0.10; declared in `manifest.json` before predictions; nested across checkpoints | Same rate as the pilot; nested so verified texts are reused |
| Grouping | Unchanged Jev grouping role on complaint/cancellation texts | |
| Memo | Separate lightweight generative model (OpenAI, `OPENAI_API_KEY`), one bounded call per checkpoint; not part of the Jev runner | Pending adapter and model decision |
| Keys | Pipeline loads only `TYPESAFE_API_KEY` (Jev) and `OPENAI_API_KEY` (memo) from `.env`; never printed or logged | Billed to provider API credits, not to any subscription |

## Run-wide stop conditions (enforced by `review_pipeline/scale.py`)

1. Spent + reserved + next reservation would exceed the cap.
2. Any unknown provider outcome (timeout, 5xx, invalid JSON, missing usage): reservation held; resume refused until reconciled.
3. Any request costing more than its $0.01 reservation.
4. Invalid outputs above 2% of requests (minimum 3), or 3 contract-validation failures. Each task gets at most one invalid-output retry.
5. More than 20 HTTP 429 responses (each one backs off with jitter or `Retry-After`, recorded as unbilled and not counted as an attempt).
6. Any other 4xx (for example auth or insufficient credit).
7. Quarantined tasks above 1% of a stage.

## Checkpoint A: 500 reviews

| | |
| --- | --- |
| Input | `data/source/checkpoint_500.csv` (instructor file, SHA-256 `a94e3166…a12f`) |
| Work (measured) | 500 rows, 479 unique texts (21 duplicates), 52 verified reviews |
| Workers | 2, at most 4 requests per second |
| Interruption demo | First invocation stops after 20 saved batches; second resumes (record the screen) |
| Projected cost | $0.035 base / $0.042 conservative (Jev ≈ $0.030 + memo ≈ $0.005) |
| **Hard cap** | **$0.25** |

```sh
python3 -m review_pipeline.scale --input data/source/checkpoint_500.csv --name checkpoint_500 \
  --expected-rows 500 --budget-usd 0.25 --workers 2 --stop-after-batches 20 --execute-paid
python3 -m review_pipeline.scale --input data/source/checkpoint_500.csv --name checkpoint_500 \
  --expected-rows 500 --budget-usd 0.25 --workers 2 --execute-paid
```

**Gate before Checkpoint B:** zero unknown costs; at most 1% quarantined; measured enrichment cost per unique text no more than 1.5× pilot (≈ $0.000075); verifier joint topic/intent/severity agreement at least 80% (pilot 9/10; same-model agreement, not accuracy); refreshed 100,000 projection no more than $8 conservative.

## Checkpoint B: 10,000 reviews

| | |
| --- | --- |
| Input | `data/source/analysis_10000.csv` (contains all of Checkpoint A) |
| Work (measured) | 10,000 rows, 8,448 unique texts; 7,969 new after A. 986 verified reviews, 851 new unique |
| Workers | 4, at most 8 requests per second (raise only if A showed no 429s) |
| Projected cost | $0.50 incremental base / $0.63 conservative |
| **Hard cap** | **$1.50** |

```sh
python3 -m review_pipeline.scale --input data/source/analysis_10000.csv --name analysis_10000 \
  --expected-rows 10000 --budget-usd 1.50 --workers 4 --requests-per-second 8 --execute-paid
```

**Gate before 100,000:** same gates as A on the larger sample, plus measured throughput per worker and rate-limit count to choose 4–8 workers, and a measured complaint share to refresh grouping cost.

## Then 100,000 (separate approval later)

Input `data/scale/sample_100000.csv` (SHA-256 `cf2641e8f3075e392a4c693c7cd45484c68e907569c48718cf131f0e321d643c`): 78,099 unique texts; 69,651 new after B. Projected $4.38 incremental base / about $5.5 conservative, total sample $4.91 / $6.14. Proposed cap $8, under the $10 target.

## Evidence outputs per checkpoint (`artifacts/scale/<name>/`)

`manifest.json` (input SHA, config IDs, cap, declared verification IDs), `calls.jsonl` (every attempt: role, worker, model, review IDs, tokens, cost, outcome, raw response), `records.jsonl`, `verification.jsonl`, `verification_comparison.json`, `membership.csv`, `ranking.csv`, `checkpoint_before.json` / `checkpoint_after.json`, `run.json` (ledger, remaining budget, cost by role, cache reuse, stage times, workers, counters, stop reason).

## Approval requested

| Checkpoint | Provider / model | Charged to | Projected | Hard cap |
| --- | --- | --- | ---: | ---: |
| A: 500 | TypeSafe Jev `jev-1.13.0` | TypeSafe API credits (`TYPESAFE_API_KEY`) | $0.035 | $0.25 |
| B: 10,000 | TypeSafe Jev `jev-1.13.0` | TypeSafe API credits | $0.50 | $1.50 |
| Memo (each, later) | OpenAI `gpt-4.1-mini-2025-04-14` or GPT-6 Luna | OpenAI API credits (`OPENAI_API_KEY`) | ≈ $0.005 | separate, ≤ $0.10 |

Maximum combined exposure for A + B: **$1.75**. Credit balances must be confirmed by Tito in the provider consoles; the project cannot verify them without a billed call.
