"""Build an offline measured-pilot report and explicitly estimated projections.

No network or provider imports. Optional cold metadata: budget_usd,
max_workers, output_token_cap, maximum_fallback_fraction, input_sha256.
Stage times must be nonoverlapping elapsed stage times, not summed requests.
"""
import argparse
import json
import math
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .cost import replay


STAGES = {"prepare", "enrich", "verify", "group", "rank", "memo"}
ALIASES = {"ingest": "prepare", "classify": "enrich", "recommend": "memo"}


def _read(path, fallback):
    return json.loads(path.read_text()) if path.exists() else fallback


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _valid_number(value, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _phase_summary(phase, metadata, priced, records):
    calls = [call for call in priced if call["run_phase"] == phase]
    missing = []
    stages = {ALIASES.get(stage, stage) for stage in metadata.get("stages_completed", [])}
    if not STAGES.issubset(stages):
        missing.append("missing completed stages: " + ", ".join(sorted(STAGES - stages)))
    if metadata.get("phase") != phase:
        missing.append("missing or mismatched metadata phase")
    elapsed = metadata.get("elapsed_seconds")
    if not _valid_number(elapsed, positive=True):
        missing.append("missing positive end-to-end elapsed_seconds")
        elapsed = None
    count = metadata.get("input_count")
    if count != 100:
        missing.append("required fixed pilot input_count is 100")
    statuses = dict(Counter(record.get("status", "missing") for record in records))
    if len(records) != count or len({record.get("review_id") for record in records}) != len(records):
        missing.append("records do not reconcile to unique input IDs")
    if any(not record.get("review_id") for record in records):
        missing.append("missing record ID")
    if metadata.get("status_counts") != statuses:
        missing.append("status_counts do not reconcile to saved records")
    if statuses.get("completed", 0) != count:
        missing.append("pilot has uncompleted records")
    if phase == "cold":
        for role in ("enrich", "verify", "group", "memo"):
            if not any(call["role"] == role and call["outcome"] == "succeeded" for call in calls):
                missing.append("no successful " + role + " call evidence")
    enrich_calls = sum(call["role"] == "enrich" for call in calls)
    if phase == "warm" and enrich_calls:
        missing.append("warm run made new enrichment calls")
    unknown = sum(call["calculated_cost_usd"] is None for call in calls)
    subtotal = sum((Decimal(call["calculated_cost_usd"]) for call in calls
                    if call["calculated_cost_usd"] is not None), Decimal(0))
    if unknown:
        missing.append("unresolved call usage or rates")
    complete = not missing
    duration_basis = metadata.get("runtime_basis", "end_to_end")
    active = metadata.get("active_elapsed_seconds")
    if active is None and duration_basis == "active_attempts":
        active = elapsed
    wall = metadata.get("wall_elapsed_seconds")
    if wall is None and duration_basis == "end_to_end":
        wall = elapsed
    active = active if _valid_number(active) else None
    wall = wall if _valid_number(wall) else None
    return {
        "complete": complete, "incomplete_reasons": missing, "metadata": metadata,
        "saved_status_counts": statuses, "attempts": len(calls),
        "failed_attempts": sum(call["outcome"] == "failed" for call in calls),
        "new_enrichment_calls": enrich_calls, "unknown_cost_attempts": unknown,
        "known_api_cost_usd": str(subtotal),
        "total_api_cost_usd": str(subtotal) if not unknown and (calls or complete) else None,
        "elapsed_seconds": elapsed,
        "runtime_basis": duration_basis,
        "active_elapsed_seconds": active,
        "wall_elapsed_seconds": wall,
        "pause_seconds": wall - active if wall is not None and active is not None and wall >= active else None,
        "input_rows_per_second": count / elapsed if elapsed and _valid_number(count) else None,
        "api_cost_per_1000_inputs_usd": str(subtotal * 1000 / count) if complete else None,
        "api_cost_per_completed_record_usd": str(subtotal / count) if complete else None,
        "local_compute_cost_usd": None,
    }


def build_report(directory, full_nonempty=660609, full_unique=484189):
    """Read saved pilot files; write report.json/report.md and return the report.

    Projection uses observed stage cost, including failed attempts, and observed
    verification/complaint fractions. It assumes unchanged configuration and mix.
    It is withheld for incomplete evidence; no missing cost becomes zero.
    """
    if (not isinstance(full_nonempty, int) or isinstance(full_nonempty, bool)
            or not isinstance(full_unique, int) or isinstance(full_unique, bool)
            or not 0 < full_unique <= full_nonempty):
        raise ValueError("require positive integer full_unique <= full_nonempty")
    root = Path(directory)
    records = _lines(root / "pilot_records.jsonl")
    raw_calls = _lines(root / "pilot_calls.jsonl")
    cold = _read(root / "cold.json", {})
    warm = _read(root / "warm.json", {})
    with tempfile.TemporaryDirectory() as temp:
        pricing = replay(root / "pilot_calls.jsonl", root / "rates.csv", Path(temp) / "replay.json")
    actual = {phase: _phase_summary(phase, meta, pricing["calls"], records)
              for phase, meta in (("cold", cold), ("warm", warm))}
    projection = {"available": False, "full_nonempty": full_nonempty, "full_unique": full_unique,
                  "scenarios": [], "reasons": []}
    unique = cold.get("unique_nonempty_texts")
    if not actual["cold"]["complete"]:
        projection["reasons"].append("cold pilot evidence incomplete")
    if not isinstance(unique, int) or isinstance(unique, bool) or not 0 < unique <= len(records):
        projection["reasons"].append("missing valid pilot unique_nonempty_texts")
    if any(call["run_phase"] not in ("cold", "warm") for call in pricing["calls"]):
        projection["reasons"].append("unassigned call phases")
    known_ids = {record.get("review_id") for record in records}
    verified_ids = {review_id for call in raw_calls
                    if call.get("run_phase") == "cold" and call.get("role") == "verify"
                    and call.get("outcome") == "succeeded" for review_id in call.get("review_ids", [])}
    if not verified_ids or not verified_ids.issubset(known_ids):
        projection["reasons"].append("verification sample IDs missing or foreign")
    complaint_originals = {record.get("cache_source_id") or record.get("review_id") for record in records
                           if record.get("status") == "completed"
                           and record.get("intent") in ("complaint", "cancellation")}
    if not complaint_originals:
        projection["reasons"].append("no observed complaint examples to extrapolate grouping")
    unknown_roles = {call["role"] for call in pricing["calls"] if call["run_phase"] == "cold"} - {"enrich", "verify", "group", "memo"}
    if unknown_roles:
        projection["reasons"].append("unmodeled call roles: " + ", ".join(sorted(unknown_roles)))
    if not projection["reasons"]:
        stage_cost = {role: sum((Decimal(call["calculated_cost_usd"]) for call in pricing["calls"]
                                if call["run_phase"] == "cold" and call["role"] == role), Decimal(0))
                      for role in ("enrich", "verify", "group", "memo")}
        verify_rate = len(verified_ids) / cold["input_count"]
        complaint_rate = len(complaint_originals) / unique
        times = {ALIASES.get(key, key): value for key, value in cold.get("stage_times_seconds", cold.get("stage_seconds", {})).items()}
        time_valid = (STAGES.issubset(times) and all(_valid_number(value) for value in times.values())
                      and sum(times.values()) <= cold["elapsed_seconds"] + 0.001)
        overhead = max(0, cold["elapsed_seconds"] - sum(times.values())) if time_valid else None
        for reuse, work in ((True, full_unique), (False, full_nonempty)):
            factors = {"enrich": work / unique, "verify": full_nonempty / cold["input_count"],
                       "group": work / unique, "memo": 1}
            for label, multiplier in (("base", 1), ("conservative", 1.25)):
                costs = {role: stage_cost[role] * Decimal(str(factor)) * Decimal(str(multiplier if role != "memo" else 1))
                         for role, factor in factors.items()}
                total = sum(costs.values(), Decimal(0))
                elapsed = None
                if time_valid:
                    elapsed = overhead + times["memo"] + sum(times[role] * factors[role] * multiplier
                              for role in ("enrich", "verify", "group"))
                    elapsed += (times["prepare"] + times["rank"]) * full_nonempty / cold["input_count"]
                budget = cold.get("budget_usd")
                projection["scenarios"].append({
                    "name": label + ("_exact_reuse" if reuse else "_no_reuse"),
                    "estimated": True, "api_cost_usd": str(total),
                    "stage_api_cost_usd": {key: str(value) for key, value in costs.items()},
                    "elapsed_seconds": elapsed, "enrichment_work_items": work,
                    "runtime_basis": cold.get("runtime_basis", "end_to_end"),
                    "expected_verified_records": full_nonempty * verify_rate,
                    "expected_unique_complaint_grouping_items": work * complaint_rate,
                    "variable_stage_multiplier": multiplier,
                    "over_budget": float(total) > budget if _valid_number(budget) else None,
                    "local_compute_cost_usd": None,
                })
        projection.update(available=True, observed_verification_rate=verify_rate,
                          observed_unique_complaint_fraction=complaint_rate,
                          runtime_estimate_available=time_valid)
    report = {
        "version": "pilot-report-v1", "actual": actual, "projection": projection,
        "pilot_complete": all(item["complete"] for item in actual.values()),
        "call_evidence": pricing, "assumptions": [
            "Projections are estimates from 100 reviews, not executed full-run results; refresh at 500 and 10000.",
            "Same model configuration, worker limit, review mix, verification fraction and observed retry/fallback mix are assumed.",
            "Exact-result reuse applies to enrichment/grouping; random verification remains over original IDs.",
            "Grouping scales with observed unique complaint share; memo cost occurs once and is not multiplied by review count.",
            "Conservative scenarios add 25% variable model cost/time for higher retries/fallbacks; this is an editable planning assumption in code, not measured overhead.",
            "Runtime requires nonoverlapping stage elapsed times; no linear parallel speedup is assumed.",
            "Active-attempt runtime excludes pauses between invocations; wall elapsed and pause duration are shown only if separately measured.",
            "Missing local compute costs remain unknown. Pilot completion does not certify source checksum, human labels or grading compliance.",
        ],
    }
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Offline pilot cost and runtime report", "", f"Complete pilot evidence: {report['pilot_complete']}", "",
             "## Saved measurements", "", "| Run | Attempts | Known API cost (USD) | Unknown-cost attempts | Measured seconds | Runtime basis | Complete |",
             "| --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for phase, item in actual.items():
        lines.append(f"| {phase} | {item['attempts']} | {item['known_api_cost_usd']} | {item['unknown_cost_attempts']} | {item['elapsed_seconds']} | {item['runtime_basis']} | {item['complete']} |")
    lines += ["", "Known subtotals exclude unresolved costs. Local compute costs are unknown.", ""]
    for phase, item in actual.items():
        if item.get('wall_elapsed_seconds') is not None:
            lines.append(f"- {phase} measured wall window: {item['wall_elapsed_seconds']:.3f} seconds; paused/between-command time: {item['pause_seconds']:.3f} seconds." if item.get('pause_seconds') is not None else f"- {phase} measured wall window: {item['wall_elapsed_seconds']:.3f} seconds.")
    lines += ["", "## Estimated scenarios", ""]
    for scenario in projection["scenarios"]:
        duration = 'unknown' if scenario['elapsed_seconds'] is None else f"{scenario['elapsed_seconds'] / 3600:.2f} hours"
        lines.append(f"- {scenario['name']}: ${Decimal(str(scenario['api_cost_usd'])):.2f} API; estimated active runtime: {duration}; over budget: {scenario['over_budget']}.")
    if not projection["available"]:
        lines.append("Projection unavailable: " + "; ".join(projection["reasons"]) + ".")
    lines += ["", "## Evidence gaps", ""]
    for phase, item in actual.items():
        lines.extend(f"- {phase}: {reason}" for reason in item["incomplete_reasons"])
    lines += ["", "## Assumptions", ""] + ["- " + text for text in report["assumptions"]]
    lines += ["", "Detailed stage costs, calls, rates, metadata and source links are in report.json.", ""]
    (root / "report.md").write_text("\n".join(lines))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--full-nonempty", type=int, default=660609)
    parser.add_argument("--full-unique", type=int, default=484189)
    arguments = parser.parse_args()
    build_report(arguments.directory, arguments.full_nonempty, arguments.full_unique)
