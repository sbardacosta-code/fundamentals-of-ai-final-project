# Jev-centered multi-agent project plan

## Latest instructor update — October 11, 2026

The student pasted Pepe’s new announcement, preserved verbatim in `research/instructor-update-2026-10-11.json`. It extends the deadline to “oct 20th midnight” and explicitly discusses **100,000-review runs costing no more than $10**, using caching, batching, parallelization, queues, and Jev/GPT decisions combined with a lightweight generative model such as Luna or Haiku. This supersedes the older full-corpus planning target below: plan a reproducible 100,000-review run, retaining full-file profiling and truthful scope reporting. The legacy checker still uses full-corpus denominators; disclose that mismatch rather than rewriting the instructor’s supplied files or claiming full-corpus completion. Exact deadline timezone/boundary is not specified in the pasted text.

Jev remains central. The current memo model is a separate lightweight generative model; Luna/Haiku are candidates for a measured comparison, not silently substituted into earlier evidence. The $10 is an instructor target, not a new authorization to charge the user: existing pilot accounting and authorization remain unchanged.

A preliminary projection from the saved baseline is approximately **$6.25 base / $7.81 with a 25% variable-cost buffer for 100,000 reviews**, even without exact-text savings. It assumes the pilot’s token/complaint mix, 10%verification and one memo. It is neither measured 100k spending nor a guaranteed upper bound. Freeze a deterministic representative 100k sample, measure its duplicate count and refresh the calculator after 500/10,000-review checkpoints before scale-up. Human golden labels remain independent and must not be used to tune prompts.

Earlier estimates and requirements below are historical unless compatible with this update.

Updated October 8, 2026. Planning only; no paid calls or classifications have been run. This plan supersedes the earlier provider-neutral proposal. Jev is the chosen primary decision engine. The purpose is to demonstrate a measured, auditable multi-agent workflow over Spotify reviews.

## Workflow and roles

Architecture: a deterministic orchestrator, bounded parallel workers within stages, a chain between dependent stages, and an optional capped advisor branch. A swarm is unnecessary because the work has stable inputs, shared resource limits and explicit dependencies. Separate role instructions, input/output contracts, logs and saved handoffs establish the distinct agents; several questions in one API request do not by themselves establish multiple agents.

| Role | Engine | Input → saved output |
|---|---|---|
| Prepare and orchestrate | Code/database | CSV → original rows/hashes, profile, unique-text mapping, queued tasks and run manifest |
| Enrichment agent | Jev | Exact review text and rubric → topic, intent, severity, sentiment and uncertainty signals, raw answers/probabilities |
| Verification agent | Jev, separate blind task | Declared random sample of original texts and rubric → independent predictions; code produces disagreements |
| Grouping agent | Jev | Complaint text and versioned issue definitions → stable issue assignment and membership evidence |
| Advisor | Generative model, capped | Difficult original review and rubric → structured adjudication, preserved separately from first predictions |
| Rank | Code/database | Completed complaint/cancellation membership → exact baseline aggregates and rankings |
| Recommendation agent | Generative model | Saved ranks, numbers and source evidence → memo/recommendations with issue/review/claim IDs |

The generative provider is still undecided; its scope is limited to prose, optional bounded issue-definition discovery and difficult cases. Jev remains responsible for routine semantic decisions at scale. The verifier's use of the same model is procedural independence, not independent model errors; report correlated-error limitations and compare against the student's human golden labels.

## Jev decision design

- Pin the validated version (currently documented as `jev-1.13.0`) and log the returned version. Never silently switch models or reuse results across changed configurations.
- Topic: Choice over the eight exact contract labels. Intent: Choice with the contract's precedence. Severity: Choice over five described impact levels, mapped by code to integers 1–5.
- Sentiment: Score over five explicitly described levels; code maps its 0–4 result to −1…1 with `score / 2 - 1`. This is a declared operational scale, not a precisely measured psychological quantity. Evaluate with predeclared MAE/tolerance.
- Cancellation/ambiguity: use Noul only for explicit, useful propositions. Preserve raw probabilities; thresholds are chosen on development cases and frozen before golden evaluation. Confidence is a signal, not proof of correctness.
- Evidence: code preserves exact text and extracts candidate spans; the complete short review can serve as its quote. When selecting among spans, Jev chooses a candidate and code returns the exact original substring. Explicit term matching can populate entities; unsupported additions fail validation. Route weak evidence for inspection.
- Grouping has its own rubric/configuration and saved role calls. Start with a compact issue catalog grounded in development examples. Include a residual issue per topic so every completed complaint/cancellation receives membership. Freeze definitions before full assignment. If discovery changes the catalog, version it and invalidate affected grouping/ranking/memo caches. Keep the required eight topics unchanged.

## The four required techniques

1. **Queues:** one authoritative durable work ledger, with stage/status fields, atomic claims, leases, attempt counts and idempotent commits. Bounded pending/in-flight work provides backpressure. Persist request outcomes and every validated batch. On restart, reclaim expired work cautiously and dispatch pending tasks; reconcile unknown request outcomes separately.
2. **Caching:** exact-text + role + model + prompt/rubric/schema/settings key. Preserve each source ID and direct `cache_source_id` pointing to the original model-evaluated result. Duplicate texts retain separate business counts. Independent verification cannot read enrichment predictions/cache. Downstream caches include issue-catalog and input-artifact hashes. Provider prompt caching is separate and savings are recorded only when supported by usage evidence.
3. **Parallelization:** Jev workers process independent queued tasks concurrently, under a shared request/token limiter and spend ledger with in-flight reservations. The required cold pilot uses one worker; then measure two before increasing. Dependent stages wait for the necessary saved inputs. Benchmark comparable inputs/cache conditions; do not claim hypothetical speedups as measurements.
4. **Batches:** stream CSV chunks; combine several Jev questions per review; test modest multi-review packs (e.g. 5 then 10) with a hard 50-review/request ceiling and token checks. Put the exact review path in every question's instructions: API question keys alone are not interpreted as instructions. Compare packed decisions against single-review development cases and test isolation with conflicting/injected neighboring reviews. Reduce batch size if quality drops. Provider asynchronous Batch pricing is a separate optional feature and is not assumed to be available for Jev.

Shared controls: one invalid-output retry; bounded transient-error backoff/jitter; output/timeout limits for generative calls; global spending ceiling; advisor quota; transactional outputs. Stop dispatch when spent plus reserved plus next reservation exceeds the configured cap. Unknown timeout charges remain explicitly unresolved. On October 10 the user authorized up to $5 for the first real 100-review pilot, including supporting calls; full-run spending remains unapproved. See config/pilot.json.

## Implementation sequence and acceptance evidence

### 1. Foundation and human evaluation setup

Profile the entire checksum-verified CSV; preserve original IDs and values. Implement queue/cache/database, Jev adapter, role schemas and orchestration. Prepare a local labeling interface with blank golden answers and exact rubric definitions for the student. Do not auto-fill human labels. Build offline calculator and grading export interfaces early.

Acceptance: deterministic full-input profile; invalid input and duplicate handling; demonstrable atomic task claims/cache invalidation; no paid calls in imports/offline commands. Isolate synthetic fixtures from business data and label them clearly.

### 2. Establish Jev behavior, then run the fixed 100

After access and budget are configured, use development examples to check label boundaries and batch isolation. Freeze initial settings, then run unchanged `cost_100.csv` cold with one worker, all stages and a predeclared verification sample. Repeat warm and prove zero new enrichment calls. Record every attempt, raw usage, fees, failures and measured wall time. Choose a small initial random verification sample (proposed 10 of 100) before examining predictions; targeted difficult-case checks are a separate stratum.

Acceptance: offline replay recomputes actual costs; editable rates do not change measured timing; doubled rates double only API spend. Report Jev, verifier, grouping, advisor and memo work separately. Set exact dated provider rates before paid execution. Reject invalid IDs/fields and preserve failures.

### 3. Evaluate and scale

Run 500 then 10,000 checkpoints, refresh cost/runtime projections, inspect disagreements and failures. Student completes all 50 human labels before final evaluation; keep them out of prompts, threshold selection and issue discovery. Record topic/intent agreement, severity exact agreement/MAE, sentiment error, quote support, unsupported entities and uncertainty behavior. Test planted errors, prompt injection, option-order sensitivity, temporary failures and recovery. Record an interruption/resume demonstration with before/after completed-ID snapshots.

Default design target: all 660,622 IDs, 660,609 nonempty classifications, 13 explicit empty quarantines. Use exact-text reuse for expected 484,189 unique nonempty texts, while measuring actual counts. The brief's 100,000 allowance conflicts with full-coverage grading denominators; do not change grading math. Resolve reduced-scope grading only if budget/access make a full run impractical.

### 4. Analysis, deployment and submission

Code computes baseline priority = severity sum, sorts descending score then ascending issue ID, and uses six-decimal half-up means. The recommendation agent receives calculated numbers and selected evidence, never the full corpus. Validate referenced IDs and material numeric claims against saved claims/aggregates; preserve errors and limitations.

Deploy a database and backend that serve saved results to a dashboard. Show business priorities and traceable review evidence, plus a pipeline view of role handoffs, coverage, queues, cache reuse, batch sizes, concurrency, disagreement, cost and recovery. Keep the primary user view simple. Export `grading/` and `cost/`, run the supplied checker and offline replay, test the live backend/database path, and link all evidence from the public repository README. Due October 13, 11:59 p.m. Pacific; resolve the portal vs bCourses submission-route discrepancy before submitting.

## Official implementation references checked October 8

- [API request shape and typed outputs](https://docs.typesafe.ai/api): state and named typed questions, raw usage, and answer mapping. Explicit question instructions carry row targeting; names alone do not.
- [Parallel questions](https://docs.typesafe.ai/patterns/fan-out): combine independent semantic questions, then route in code.
- [Known Jev limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13): explicit criteria, small relevant context, option-order and injection tests; code owns arithmetic, generative models own prose.
- [Versions and pricing](https://docs.typesafe.ai/models): pin the deployed version and recheck account access/rates before paid execution.

The project requirements remain in `requirements.md`; class guidance and source conflicts remain in `class-notes.md`. These documents are plans and source summaries, not fabricated run evidence.
