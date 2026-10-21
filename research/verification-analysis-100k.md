# Blind verification analysis: 100,000-review run

Source: `cost/scale/sample_100000/verification.jsonl.gz` and `verification_comparison.json`. The verifier is the same Jev model re-classifying a declared 10% random sample from scratch without seeing first-pass labels. Agreement measures **consistency**, not accuracy; human golden labels are still required for accuracy.

## Headline numbers (measured)

| Measure | Value |
| --- | ---: |
| Verified reviews | 9,996 |
| Full agreement on topic, intent and severity | 8,739 (87.4%) |
| Topic disagreements | 482 (4.8%) |
| Intent disagreements | 370 (3.7%) |
| Severity disagreements | 528 (5.3%) |
| Severity off by 1 / 2 / 3 levels | 483 / 37 / 8 |
| Mean absolute severity difference | 0.058 |

## Where the two passes disagree

| Field | Most common first pass → verifier pairs |
| --- | --- |
| Topic | other→usability (44), catalog→other (40), playback→usability (36), billing↔usability (35 each way), usability→other (35) |
| Intent | praise→unclear (88), unclear→praise (69), unclear→complaint (42), complaint→unclear (33), complaint→cancellation (23), request→complaint (20) |
| Severity | 2→3 (100), 3→2 (90), 4→3 (82), 2→1 (78), 1→2 (75), 3→4 (57) |

Disagreements cluster at real rubric boundaries: very short or vague texts ("Cool", "it is bed") between praise, unclear and complaint; usability versus billing for ad and paywall complaints; and adjacent severity levels. Large severity jumps are rare (45 of 9,996 differ by 2 or more).

Short examples (review text is public CC0 data):

| Field | Review | First pass | Verifier |
| --- | --- | --- | --- |
| topic | Forces you to listen to music | usability | other |
| topic | What happen to the song … I down loaded before n I can't find the song anymore. | downloads | catalog |
| intent | it is bed | unclear | complaint |
| intent | Cool | praise | unclear |
| intent | I boycott this app because [political reason] | cancellation | unclear |
| severity | I can't even listen to my playlist it keeps playing random songs | 3 | 4 |

## Does the ambiguity flag help?

| First-pass `needs_review` | Verified | Any disagreement | Rate |
| --- | ---: | ---: | ---: |
| true | 2,782 | 473 | 17.0% |
| false | 7,214 | 784 | 10.9% |

The ambiguity flag roughly 1.6× enriches disagreements, so it is a useful triage signal but not a reliable error detector: most disagreements (784 of 1,257) were not flagged.

## Limitations

- Same model, same rubric: correlated errors are invisible here. Human golden labels and the instructor's private sample are the accuracy checks.
- The boycott example shows a policy boundary (bare boycott slogans are `unclear` unless they state personal departure) where the two passes differ; it is listed for human review, not silently corrected.
- Synthetic robustness tests (`tests/test_robustness.py`) show a planted verifier disagreement is surfaced exactly and that injected instructions inside review text never reach question instructions.
