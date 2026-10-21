# Final-project requirements and source reconciliation

## Latest instructor update — October 11, 2026

The student pasted Pepe’s new announcement, preserved verbatim in `research/instructor-update-2026-10-11.json`. It extends the deadline to “oct 20th midnight” and explicitly discusses **100,000-review runs costing no more than $10**, using caching, batching, parallelization, queues, and Jev/GPT decisions combined with a lightweight generative model such as Luna or Haiku. This supersedes the older full-corpus planning target below: plan a reproducible 100,000-review run, retaining full-file profiling and truthful scope reporting. The legacy checker still uses full-corpus denominators; disclose that mismatch rather than rewriting the instructor’s supplied files or claiming full-corpus completion. Exact deadline timezone/boundary is not specified in the pasted text.

Jev remains central. The current memo model is a separate lightweight generative model; Luna/Haiku are candidates for a measured comparison, not silently substituted into earlier evidence. The $10 is an instructor target, not a new authorization to charge the user: existing pilot accounting and authorization remain unchanged.

A preliminary projection from the saved baseline is approximately **$6.25 base / $7.81 with a 25% variable-cost buffer for 100,000 reviews**, even without exact-text savings. It assumes the pilot’s token/complaint mix, 10%verification and one memo. It is neither measured 100k spending nor a guaranteed upper bound. Freeze a deterministic representative 100k sample, measure its duplicate count and refresh the calculator after 500/10,000-review checkpoints before scale-up. Human golden labels remain independent and must not be used to tune prompts.

Earlier estimates and requirements below are historical unless compatible with this update.

Reviewed October 6, 2026. This is repository planning documentation, not proof of any executed pipeline or evaluation.

Project decision updated October 8: the user requires **Jev as the primary decision engine**, with multi-agent workflows as the central learning objective. Follow `research/jev-workflow-plan.md` for the implementation plan. Provider flexibility in the assignment is background information, not a reason to replace this project choice.

## Sources and review coverage

- Current written [assignment](https://docs.google.com/document/d/1PUFw56OGxqbUxiFm0vKtsWX8ac8633BgOlCSGa3TgLA/edit), cached in `research/cache/final-assignment.md`.
- Dataset ZIP specifications: `data/source/GRADING_CONTRACT.md` and `data/source/COST_CALCULATOR.md`. The assignment explicitly makes the grading contract the required export format.
- Public [course index](https://haas-ai-classes-fall-26.vercel.app/), [Class 5](https://haas-ai-classes-fall-26.vercel.app/class5.html), [Class 6](https://haas-ai-classes-fall-26.vercel.app/class6.html), and [Class 7](https://haas-ai-classes-fall-26.vercel.app/class7.html) were inspected by a parallel worker, focusing on project-relevant sections. The coordinating agent also reviewed selected relevant sections of [Class 1](https://haas-ai-classes-fall-26.vercel.app/class1.html) (explicit inputs/outputs, testable functions and reproducibility), [Class 2](https://haas-ai-classes-fall-26.vercel.app/class2.html) (backend/API boundaries, validation and durable data), [Class 3](https://haas-ai-classes-fall-26.vercel.app/class3.html) (untouched tests, validation and generalization), and [Class 4](https://haas-ai-classes-fall-26.vercel.app/class4.html) (model-specific tokenization and context cost). This was a project-focused review, not a slide-by-slide audit of every exercise and diagram.
- The coordinating agent reports reviewing Granola notes dated August 25, September 1, September 15, September 29, and October 6 (two notes); no matching September 8/22 notes were found. This file does not independently substantiate their contents.
- No golden review content or human labels were inspected or generated in this review.

## Deliverable and scope

Individual capstone; 30% of course grade; due October 13, 2026, 11:59 p.m. Pacific. Submit one public GitHub repository URL through the course portal linked in bCourses. README must link runnable code/setup, role prompts, architecture, saved outputs, evals, cost evidence, memo, deployed dashboard/access instructions, and downloadable large artifacts. Check accessibility signed out.

The current brief requires a deployed dashboard **and deployed backend and database**. Store processed reviews, aggregates and analysis outputs in the database; backend retrieves saved data; dashboard displays overall metrics, issue rankings and AI-generated recommendations supported by saved metrics and issue/review evidence. Graders must inspect results without paid reruns. Hosting/framework/provider choice is open.

The strict contract requires ingest/account for all **660,622** original IDs, classify **660,609** nonempty reviews, and quarantine **13** empty texts with `empty_review_text`. Other failures remain visible and reduce successful coverage. There are **484,189** distinct nonempty texts eligible for exact-text reuse. Development sets 100 → 500 → 10,000 are checkpoints, not final-scope substitutes under the contract.

**Unresolved 100,000 exception:** the current brief's submission list and Definition of Done add “or at least 100,000,” but its rubric, scope, starter prompt, ZIP contract and checker description continue to require the full corpus. The coverage formula remains `0.2 × full-profile match + 0.2 × valid accounted rows / 660622 + 0.6 × valid classified nonempty rows / 660609`. Do not claim that 100,000 earns full coverage, change denominators, or silently treat unprocessed reviews as completed. Plan full-file profiling and a resumable full-corpus pipeline; confirm the intended grading effect of the exception before committing to a reduced final scope or paid scale-up.

## Pipeline, queues, caching, batches and agents

Saved program accepts an arbitrary input CSV path and runs prepare → enrich → independently verify → group → rank → recommend without manual chat pasting. A fixed stage chain is acceptable. Distinct enrichment, verification, grouping and memo roles need separate instructions, inputs/outputs and saved evidence; they may share one provider/model. Recommended architecture: deterministic orchestrator with a persistent pending-work queue and bounded parallel workers inside applicable stages; no swarm is necessary.

- Code owns parsing, exact deduplication, cache lookup, dispatch, validation, retries, accounting, persistence, arithmetic and limits. Models perform bounded language judgments. Diagram each boundary and handoff.
- Enrichment requests contain **at most 50 reviews**; validate all returned IDs and save atomically after every batch. Distinguish CSV chunks, multi-review requests and optional provider Batch APIs.
- Exact-result cache keys include exact text plus model, effort, prompt and schema configuration. Preserve each original ID and its independent business count. `cache_source_id` points directly to a completed original with identical text, labels/configuration and model-call evidence; alias chains are disallowed. Similar text requires new work. Provider prompt caching is separate.
- Begin the required pilot with **one worker**; test two and expand only within measured capacity. Share global request/token limits and one spend ledger. Reserve worst-case in-flight cost before dispatch; stop when spent + reserved + next reservation exceeds cap. Bound backoff/jitter, time, output tokens and stronger-model fallback fraction. Invalid-output retries are limited to one. Reconcile uncertain timeout charges.
- Demonstrate interruption after saved progress, then resume to additional completed records with no new enrichment calls for already-completed IDs under unchanged configuration. Supply a short recording and before/after checkpoints. Cache downstream stages by their inputs/configuration or disclose reruns.

## Common labels and record contract

Exact primary topics: `access`, `usability`, `playback`, `downloads`, `catalog`, `billing`, `support`, `other`. Intents in precedence order: `cancellation`, `complaint`, `request`, `praise`, `unclear`. Choose the highest-supported-severity problem, first mentioned on a tie; positive reviews use the first specific praised feature. Paid-plan mention alone is not billing. Classify text, not stars.

Severity: 1 no reported problem/pure request; 2 annoyance/generic criticism; 3 degraded function with some use remaining; 4 clearly blocked core task; 5 explicit serious financial/privacy/data harm. Cancellation, anger and stars alone do not raise severity. Add explanatory examples; flag missing context without inventing impact.

Completed record fields: `review_id, source_sha256, status, topic, intent, sentiment, severity, entities, evidence_quote, needs_review, label_config`. Sentiment is −1…1; severity integer 1…5; evidence quote must be an exact original-text substring. Quarantines require ID, original row hash, status and nonempty reason. Use supplied `row_sha`: SHA-256 of compact UTF-8 JSON containing the six unchanged original field strings in order `review_id, review_text, review_rating, review_likes, app_version, review_timestamp`.

## Required grading export and deterministic baseline

Export `grading/` with:

| File | Required evidence |
| --- | --- |
| `run.json` | `version: a5-audit-v1`, input identity/count, `classification_input_fields: [review_text]`, multi-issue policy |
| `ingestion.json` | Full-file profile from supplied helper |
| `records.jsonl` | Exactly one final completed/quarantined record per original ID; gzip allowed |
| `membership.csv` | `issue_id,review_id`; no duplicate pair |
| `ranking.csv` | `rank,issue_id,complaint_count,severity_sum,mean_severity,priority_score` |
| `claims.csv` | `claim_id,issue_id,metric,value` for material issue-level memo numbers |
| `calls.jsonl` | Actual request ID, role, sent review IDs, exact model, phase, outcome, label_config and usage |
| `checkpoint_before.json`, `checkpoint_after.json` | Saved `completed_ids` proving additional completed work after resume |

Call roles: `enrich`, `verify`, `group`, `memo`; enrichment phases `initial`/`resume`; outcomes `succeeded`/`failed`. Cached reuse is not a new model call. Missing usage must be visibly unavailable, never invented. Group/memo may use empty review-ID lists when their saved aggregate inputs are identified in the full log.

Baseline includes **completed complaint/cancellation records only**, each in at least one issue; normally one issue per review. Multiple issues require `allow_multi_issue:true` and disclosure that totals overlap. `complaint_count = membership count`; `severity_sum = sum(member severity)`; `mean_severity = severity_sum / complaint_count`; **`priority_score = severity_sum`**, equivalent to count × mean without rounding loss. Sort descending score, then ascending issue ID; rank from 1. Means: six decimal places, decimal half-up. Other numeric exports: integer strings. Cite `claims.csv` claim IDs in the memo. Supported checker metrics are those four aggregates. Extra business rankings are optional additions, not replacements.

Run supplied checker profile/reference/check commands without APIs; keep reference and self-check report outside grading export. Mechanical success does not establish semantic label accuracy.

## Evaluation and calculator evidence

Keep all 50 human golden answers out of prompts, examples, routing thresholds and issue discovery; original review texts still receive classifications in the final run. Hand-label before final evaluation. Tune on development data. If golden feedback informs revisions, disclose and add fresh holdout cases. Report topic/intent agreement, severity exact agreement and MAE, ambiguity counts, sentiment tolerance/MAE, quote validity/support and entity/needs-review failures. No minimum accuracy target is imposed; thoughtful error analysis matters.

Verifier relabels a declared random sample blind to the first predictions; compare in code. Save disagreement analysis, planted mislabel detection, synthetic injection tests, invalid-output/API-failure handling and reproducible reranking/recovery evidence. Synthetic cases do not enter business aggregates.

Required `cost/` artifact: runnable calculator, README with offline-default replay and separate explicit paid execution command, `pilot_records.jsonl`, `pilot_calls.jsonl`, editable dated `rates.csv`, `usage.csv` (or readable equivalents), and report. Run the unchanged fixed 100 reviews with empty result cache and one worker across all stages including declared verification; then a warm run proving **zero new enrichment calls**. Save actual attempts/failures/retries/fallbacks, IDs/hashes, settings, usage, cold/warm wall time and incremental costs. Never substitute modeled measurements.

Calculator must separate actual API billing, local-compute estimates and unknowns; use mutually exclusive billed units/rates without double-counting cached input or reasoning. Report per-stage cost/time, throughput and cost per 1,000 inputs/per completed record. Project full scope with and without exact reuse, stage-specific work counts, fixed overhead once, base/conservative retry/fallback assumptions, editable limits and budget warnings. Refresh estimates at 500/10,000. Do not extrapolate a single memo per review or confuse summed request time with wall time.

T3 evidence requires real cold/warm pilot + correct offline calculator + demonstrated retry/spending/recovery controls. Doubling API rates at fixed usage must double only API subtotal; projected-volume edits cannot change measured pilot results. Main checker does not certify costs. Overall rubric is deliverable quality 4 / evaluation 3 / working result 3, with evidence-linked README.

## Reconciled older-slide conflicts

Public Class 6/index describe the Spotify exercise; cached Class 7 retains an older taxonomy and revenue-at-risk scoring example. The current written contract controls: eight topics and severity-sum baseline. Dataset has no revenue, plan tier, confirmed churn or representative customer population; do not calculate revenue at risk or causal retention improvement. Historical self-selected reviews support expressed cancellation intent only. Class 7's Knowledge Hub expansion is explicitly optional/ungraded and is not this capstone. The current assignment's required deployed dashboard/backend/database supersedes any earlier impression that only a repository and memo were needed.

SQL, Jev, named models, additional agents, provider Batch API, alternate ranking and choice of stack are optional. User explicitly requires queues, caching, deliberate parallel-agent workflow and batches; preserve those requirements while honoring pilot and budget controls. Current provider access/settings/prices must be checked when selecting an actual implementation; slide/spec illustrative prices are not measured cost evidence.
