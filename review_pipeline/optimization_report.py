"""Offline comparison against assistant evaluation labels, never human gold."""
import argparse
import json
import math
from decimal import Decimal
from pathlib import Path

FIELDS = ("topic", "intent", "severity")
TRIALS = tuple(f"{variant}-b{size}" for variant in ("compact", "shared", "shared_short") for size in (5, 10))


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _index(rows):
    indexed = {}
    for row in rows:
        rid = row.get("review_id")
        if not rid or rid in indexed:
            raise ValueError("Missing or duplicate review_id")
        indexed[rid] = row
    return indexed


def _valid(row):
    return bool(row and row.get("status", "completed") == "completed"
                and all(field in row for field in FIELDS)
                and type(row["severity"]) is int and 1 <= row["severity"] <= 5)


def _metrics(rows, reference, baseline):
    predictions = _index(rows)
    fields = {field: {kind: {"matched_ids": [], "wrong_ids": []} for kind in ("primary", "accepted")}
              for field in FIELDS}
    primary_joint, accepted_joint, missing, baseline_joint, severe_hit = [], [], [], [], []
    severity_error, accepted_error, sentiment_errors, flagged = 0, 0, [], []
    severe = [rid for rid, ref in reference.items() if ref["severity"] >= 4]
    baseline_fields = {field: [] for field in FIELDS}
    for rid, ref in reference.items():
        pred = predictions.get(rid)
        valid = _valid(pred)
        accepted = ref.get("accepted_labels") or [{field: ref[field] for field in FIELDS}]
        if not valid:
            missing.append(rid)
        for field in FIELDS:
            for kind, match in (("primary", valid and pred[field] == ref[field]),
                                ("accepted", valid and any(pred[field] == item[field] for item in accepted))):
                fields[field][kind]["matched_ids" if match else "wrong_ids"].append(rid)
        if valid and all(pred[field] == ref[field] for field in FIELDS):
            primary_joint.append(rid)
        if valid and any(all(pred[field] == item[field] for field in FIELDS) for item in accepted):
            accepted_joint.append(rid)
        severity_error += abs(pred["severity"] - ref["severity"]) if valid else 4
        accepted_error += min(abs(pred["severity"] - item["severity"]) for item in accepted) if valid else 4
        if valid and rid in severe and pred["severity"] >= 4:
            severe_hit.append(rid)
        base = baseline.get(rid)
        if valid and _valid(base):
            for field in FIELDS:
                if pred[field] == base[field]:
                    baseline_fields[field].append(rid)
            if all(pred[field] == base[field] for field in FIELDS):
                baseline_joint.append(rid)
            p, b = pred.get("sentiment"), base.get("sentiment")
            if all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in (p, b)):
                sentiment_errors.append(abs(p - b))
        if valid and pred.get("needs_review") is True:
            flagged.append(rid)
    for field in fields.values():
        for value in field.values():
            value["accuracy"] = len(value["matched_ids"]) / 100
    return dict(denominator=100, present_valid_count=100-len(missing), missing_or_invalid_ids=missing,
                foreign_ids=sorted(set(predictions)-set(reference)), fields=fields,
                primary_joint_accuracy=len(primary_joint)/100, accepted_joint_accuracy=len(accepted_joint)/100,
                primary_joint_matched_ids=primary_joint, accepted_joint_matched_ids=accepted_joint,
                severity_mae=severity_error/100, accepted_severity_mae=accepted_error/100,
                missing_severity_policy="Missing/invalid predictions receive maximum absolute severity error 4; denominator stays 100.",
                severe_recall=len(severe_hit)/len(severe) if severe else None,
                severe_reference_ids=severe, severe_detected_ids=severe_hit,
                baseline_agreement={field: len(ids)/100 for field, ids in baseline_fields.items()},
                baseline_joint_agreement=len(baseline_joint)/100,
                sentiment_mae_vs_baseline=sum(sentiment_errors)/len(sentiment_errors) if sentiment_errors else None,
                sentiment_comparison_count=len(sentiment_errors), sentiment_missing_count=100-len(sentiment_errors),
                ambiguity_flag_rate=len(flagged)/100, ambiguity_flag_ids=flagged)


def _projection(meta, original):
    if not (meta.get("complete") and meta.get("count") == 100 and meta.get("unknown_cost_attempts") == 0):
        return {"available": False, "reason": "Requires a complete measured 100-review candidate with no unknown charges."}
    try:
        measured = Decimal(str(meta["cost_usd"]))
        unique_enrichment = meta.get("unique_enrichment_count", 100)
        if type(unique_enrichment) is not int or not 1 <= unique_enrichment <= 100:
            raise ValueError("invalid measured unique enrichment count")
        scenario = next(s for s in original["projection"]["scenarios"] if s["name"] == "base_exact_reuse")
        prior = scenario["stage_api_cost_usd"]
        unchanged = {role: Decimal(prior[role]) for role in ("verify", "group", "memo")}
        unique = original["projection"]["full_unique"]
        if not measured.is_finite() or measured < 0 or unique != 484189:
            raise ValueError("invalid cost or scope")
        enrich = measured / unique_enrichment * unique
    except (KeyError, StopIteration, ValueError, ArithmeticError):
        return {"available": False, "reason": "Missing valid measured cost or original full-run projection."}
    variable = enrich + unchanged["verify"] + unchanged["group"]
    return dict(available=True, estimated=True, full_unique=unique,
                measured_candidate_enrichment_cost_usd=str(measured),
                unique_enrichment_count=unique_enrichment,
                unique_enrichment_count_assumed="unique_enrichment_count" not in meta,
                base_stage_api_cost_usd={"enrich": str(enrich), **{role: str(value) for role, value in unchanged.items()}},
                base_api_cost_usd=str(variable + unchanged["memo"]),
                conservative_api_cost_usd=str(variable * Decimal("1.25") + unchanged["memo"]),
                assumptions=f"Candidate measured enrichment cost / {unique_enrichment} unique enrichment texts × 484189; "
                            + ("Legacy metadata lacks unique_enrichment_count: denominator 100 is assumed, not measured. " if "unique_enrichment_count" not in meta else "Denominator supplied by measured unique_enrichment_count. ")
                            + "Original verification, grouping and memo unchanged. Conservative adds 25% to variable stages only. No verification reduction.")


def build_report(directory="artifacts/optimization", baseline_directory="cost", baseline_predictions=None):
    """Write report.json/md without modifying baseline or making model calls."""
    root, base = Path(directory), Path(baseline_directory)
    reference = _index(_rows(root / "reference.jsonl"))
    if len(reference) != 100 or any(not _valid(row) for row in reference.values()):
        raise ValueError("Require exactly 100 valid assistant evaluation references")
    baseline_rows = _rows(Path(baseline_predictions) if baseline_predictions else base / "pilot_records.jsonl")
    baseline_index = _index(baseline_rows)
    baseline = _metrics(baseline_rows, reference, baseline_index)
    original = json.loads((base / "report.json").read_text())
    baseline_calls = _rows(base / "pilot_calls.jsonl")
    trials = {}
    names = sorted(path.name[:-10] for path in root.glob("*-meta.json")
                   if not any(word in path.stem.lower() for word in ("cache", "warm"))
                   and path.name[:-10] != "baseline-b5")
    for name in names:
        predictions_path, metadata_path = root / (name + ".jsonl"), root / (name + "-meta.json")
        metrics = _metrics(_rows(predictions_path), reference, baseline_index)
        metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        gates = {field: metrics["fields"][field]["accepted"]["accuracy"] >= baseline["fields"][field]["accepted"]["accuracy"] for field in FIELDS}
        gates.update({"primary_" + field: metrics["fields"][field]["primary"]["accuracy"] >= baseline["fields"][field]["primary"]["accuracy"] for field in FIELDS})
        gates["severity_mae"] = metrics["severity_mae"] <= baseline["severity_mae"]
        gates["severe_recall"] = (baseline["severe_recall"] is None or
                                   (metrics["severe_recall"] is not None and metrics["severe_recall"] >= baseline["severe_recall"]))
        gates["complete_predictions"] = metrics["present_valid_count"] == 100 and not metrics["foreign_ids"]
        gates["complete_baseline"] = baseline["present_valid_count"] == 100 and not baseline["foreign_ids"]
        projection = _projection(metadata, original) if gates["complete_predictions"] else {
            "available": False, "reason": "Candidate records are incomplete or contain foreign IDs."}
        trials[name] = dict(metrics=metrics, metadata=metadata, projection=projection,
                            provisional_gate=dict(passes=all(gates.values()), checks=gates,
                                                  automatic_promotion=False, human_quality="unknown"))
    report = dict(version="optimization-report-v1", reference_kind="assistant_audit_not_human_golden",
                  denominator=100, baseline=baseline,
                  baseline_predictions=str(Path(baseline_predictions) if baseline_predictions else base / "pilot_records.jsonl"),
                  original_cost_source_call_count=len(baseline_calls), candidates=trials,
                  limitations=["Assistant labels on 100 evaluation examples are provisional; human quality remains unknown.",
                               "Baseline agreement and sentiment similarity measure consistency, not correctness.",
                               "Accepted per-field scores permit any accepted value; joint score requires one complete accepted triple.",
                               "Severe recall uses primary reference severity >=4, with missing predictions as misses.",
                               "No automatic promotion. Costs are extrapolations; verification/grouping configuration is unchanged."])
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Provisional optimization audit", "", "Assistant labels on 100 evaluation examples, not human golden labels. Human quality remains unknown; no automatic promotion.", "",
             "| Candidate | Accepted topic | Accepted intent | Accepted severity | Accepted joint | Severity MAE | Severe recall | Provisional gate | Estimated full API USD |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |"]
    baseline_projection = {}
    if baseline_predictions:
        baseline_meta_path = Path(baseline_predictions).with_name(Path(baseline_predictions).stem + "-meta.json")
        if baseline_meta_path.exists():
            baseline_projection = _projection(json.loads(baseline_meta_path.read_text()), original)
    else:
        scenario = next((s for s in original.get("projection", {}).get("scenarios", []) if s.get("name") == "base_exact_reuse"), {})
        baseline_projection = {"base_api_cost_usd": scenario.get("api_cost_usd")}
    baseline_table = {"baseline": {"metrics": baseline, "provisional_gate": {"passes": "reference"}, "projection": baseline_projection}}
    for name, trial in {**baseline_table, **trials}.items():
        m = trial["metrics"]
        values = [str(m["fields"][field]["accepted"]["accuracy"]) for field in FIELDS]
        amount = trial["projection"].get("base_api_cost_usd")
        formatted_cost = format(Decimal(amount), ".2f") if amount is not None else "unknown"
        lines.append("| " + " | ".join([name] + values + [str(m["accepted_joint_accuracy"]), str(m["severity_mae"]), str(m["severe_recall"]), str(trial["provisional_gate"]["passes"]), formatted_cost]) + " |")
    lines += ["", "Per-field matched/wrong IDs, primary metrics, missing counts, cost assumptions and metadata are in report.json.", ""]
    lines += ["- " + limitation for limitation in report["limitations"]]
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="artifacts/optimization")
    parser.add_argument("--baseline-directory", default="cost")
    parser.add_argument("--baseline-predictions")
    args = parser.parse_args()
    build_report(args.directory, args.baseline_directory, args.baseline_predictions)
