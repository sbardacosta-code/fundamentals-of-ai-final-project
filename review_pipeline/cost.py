"""Offline usage replay; no credentials, network calls, or automatic paid execution.

Input tokens include cached input. Missing cached-input usage is unknown, not zero.
Rates describe the processing tier actually used, in USD per million tokens.
``cost_usd`` is preserved as reported evidence separately from editable-rate cost.
Call timestamps measure the call window, not end-to-end pipeline runtime.
The call-only interface cannot establish pilot coverage or stage work counts, so
it deliberately withholds extrapolation until actual pilot metadata is available.
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


RATES_TEMPLATE = (
    "model,input_per_million,output_per_million,cached_input_per_million,"
    "source_url,checked_at\n"
)


def _number(value):
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError("missing or invalid number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("number must be finite and nonnegative")
    return number


def _tokens(value):
    number = _number(value)
    if number != number.to_integral_value():
        raise ValueError("token count must be an integer")
    return number


def _money(value):
    return format(value, "f")


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _price(call, rate):
    reasons = []
    usage = {}
    for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
        try:
            usage[field] = _tokens(call.get(field))
        except ValueError:
            reasons.append("missing_or_invalid_" + field)
    if len(usage) == 3 and usage["cached_input_tokens"] > usage["input_tokens"]:
        reasons.append("cached_input_exceeds_total_input")
    if rate is None:
        reasons.append("missing_model_rates")
    if reasons:
        return None, reasons
    components = (
        (usage["input_tokens"] - usage["cached_input_tokens"], "input_per_million"),
        (usage["cached_input_tokens"], "cached_input_per_million"),
        (usage["output_tokens"], "output_per_million"),
    )
    cost = Decimal(0)
    for units, field in components:
        if units == 0:
            continue
        try:
            cost += units * _number(rate.get(field)) / Decimal(1000000)
        except ValueError:
            reasons.append("missing_or_invalid_" + field)
    return (None if reasons else cost), reasons


def _summary(items):
    known = [item for item in items if item["calculated_cost_usd"] is not None]
    reported = [item for item in items if item["reported_cost_usd"] is not None]
    intervals = []
    for item in items:
        try:
            start, end = _timestamp(item["started_at"]), _timestamp(item["ended_at"])
            if end < start:
                raise ValueError("negative duration")
            intervals.append((start, end))
        except (ValueError, TypeError, AttributeError):
            pass
    complete_times = bool(items) and len(intervals) == len(items)
    return {
        "attempts": len(items),
        "failed_attempts": sum(item["outcome"] == "failed" for item in items),
        "priced_attempts": len(known),
        "unknown_cost_attempts": len(items) - len(known),
        "calculated_api_cost_known_subtotal_usd": _money(sum(
            (Decimal(item["calculated_cost_usd"]) for item in known), Decimal(0))),
        "api_cost_complete": bool(items) and len(known) == len(items),
        "reported_cost_known_subtotal_usd": _money(sum(
            (Decimal(item["reported_cost_usd"]) for item in reported), Decimal(0))),
        "reported_cost_attempts": len(reported),
        "call_window_seconds": (
            (max(end for _, end in intervals) - min(start for start, _ in intervals)).total_seconds()
            if complete_times else None),
        "sum_request_seconds": sum((end - start).total_seconds() for start, end in intervals)
            if complete_times else None,
        "missing_or_invalid_timing_attempts": len(items) - len(intervals),
        "end_to_end_wall_seconds": None,
        "local_compute_cost_usd": None,
    }


def replay(calls_path, rates_path, output_path, volume=660622):
    """Return and write a JSON audit of all attempts, grouped by run/phase/role.

    Missing run_id/run_phase are grouped as unspecified. Unknown prices/usage
    never become free calls. Reported costs do not override recalculated costs.
    Duplicate request IDs within one run/phase raise rather than double bill.
    ``volume`` affects only the explicitly unavailable projection, never actuals.
    """
    if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
        raise ValueError("volume must be a nonnegative integer")
    with Path(rates_path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = set(RATES_TEMPLATE.strip().split(","))
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("rates CSV is missing required columns")
        rates = {}
        for row in reader:
            model = row["model"]
            if not model or model in rates:
                raise ValueError("rates require unique nonempty model IDs")
            rates[model] = row
    items, seen = [], set()
    with Path(calls_path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            call = json.loads(line)
            if not isinstance(call, dict) or not call.get("request_id"):
                raise ValueError(f"line {line_number}: missing request_id")
            run_id, phase = call.get("run_id") or "unspecified", call.get("run_phase") or "unspecified"
            identity = (run_id, phase, call["request_id"])
            if identity in seen:
                raise ValueError(f"line {line_number}: duplicate request ID within run/phase")
            seen.add(identity)
            amount, reasons = _price(call, rates.get(call.get("model")))
            reported = None
            evidence_warnings = []
            if call.get("cost_usd") is not None:
                try:
                    reported = _money(_number(call["cost_usd"]))
                except ValueError:
                    evidence_warnings.append("invalid_reported_cost_usd")
            items.append({
                "request_id": call["request_id"], "run_id": run_id, "run_phase": phase,
                "role": call.get("role") or "unspecified", "model": call.get("model"),
                "outcome": call.get("outcome"), "started_at": call.get("started_at"),
                "ended_at": call.get("ended_at"),
                "calculated_cost_usd": None if amount is None else _money(amount),
                "reported_cost_usd": reported, "unknown_cost_reasons": reasons,
                "evidence_warnings": evidence_warnings,
            })
    grouped = defaultdict(list)
    for item in items:
        grouped[(item["run_id"], item["run_phase"], item["role"])].append(item)
    report = {
        "version": "cost-replay-v1", "currency": "USD",
        "actual": _summary(items),
        "groups": [dict(run_id=key[0], run_phase=key[1], role=key[2], **_summary(value))
                   for key, value in sorted(grouped.items())],
        "calls": items, "rates": list(rates.values()),
        "projection": {"requested_volume": volume, "available": False,
                       "reason": "Call logs alone lack pilot input identity, completed counts, stage work counts, measured end-to-end time and scaling assumptions."},
        "limitations": [
            "Known subtotal excludes unknown costs; it is not a complete bill.",
            "Editable-rate calculations are distinct from provider-reported charges.",
            "Call windows exclude preparation, queue gaps outside calls, and other pipeline overhead.",
            "A zero-call warm run requires separate run metadata; empty logs do not prove a successful warm run.",
        ],
    }
    Path(output_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calls_path")
    parser.add_argument("rates_path")
    parser.add_argument("output_path")
    parser.add_argument("--volume", type=int, default=660622)
    args = parser.parse_args()
    replay(args.calls_path, args.rates_path, args.output_path, args.volume)


if __name__ == "__main__":
    main()
