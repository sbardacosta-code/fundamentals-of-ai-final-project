"""Synthetic fixtures only; none represent actual API or pilot evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from review_pipeline.cost import RATES_TEMPLATE
from review_pipeline.pilot_report import build_report


class PilotReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = [dict(review_id=str(i), status="completed", intent="complaint") for i in range(100)]
        self.calls = [dict(request_id=role, role=role, model="synthetic", run_phase="cold", phase="initial",
                           input_tokens=1000, output_tokens=100, cached_input_tokens=0,
                           outcome="succeeded", review_ids=[str(i) for i in range(10)] if role == "verify" else [],
                           started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z")
                      for role in ("enrich", "verify", "group", "memo")]
        self.metadata = dict(elapsed_seconds=10, input_count=100, unique_nonempty_texts=100,
                             status_counts={"completed": 100}, stages_completed=["prepare", "enrich", "verify", "group", "rank", "memo"],
                             stage_times_seconds={key: 1 for key in ("prepare", "enrich", "verify", "group", "rank", "memo")}, budget_usd=1)
        self.save()

    def save(self, rate=1):
        for name, rows in (("pilot_records.jsonl", self.records), ("pilot_calls.jsonl", self.calls)):
            (self.root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
        (self.root / "rates.csv").write_text(RATES_TEMPLATE + f"synthetic,{rate},{rate},{rate},https://example.invalid,synthetic\n")
        for phase in ("cold", "warm"):
            (self.root / (phase + ".json")).write_text(json.dumps(dict(self.metadata, phase=phase)))

    def test_projects_only_from_complete_evidence(self):
        report = build_report(self.root)
        self.assertTrue(report["pilot_complete"])
        self.assertEqual(report["actual"]["warm"]["total_api_cost_usd"], "0")
        self.assertEqual(len(report["projection"]["scenarios"]), 4)
        self.assertEqual(report["projection"]["observed_verification_rate"], 0.1)
        self.assertTrue((self.root / "report.md").exists())
        first = report["projection"]["scenarios"][0]
        self.assertEqual(Decimal(first["stage_api_cost_usd"]["memo"]), Decimal("0.0011"))

    def test_rates_and_volume_preserve_measurements(self):
        original = build_report(self.root)
        smaller = build_report(self.root, full_nonempty=1000, full_unique=800)
        self.assertEqual(original["actual"], smaller["actual"])
        self.save(rate=2)
        doubled = build_report(self.root)
        self.assertEqual(Decimal(doubled["actual"]["cold"]["known_api_cost_usd"]),
                         2 * Decimal(original["actual"]["cold"]["known_api_cost_usd"]))
        self.assertEqual(doubled["actual"]["cold"]["elapsed_seconds"], original["actual"]["cold"]["elapsed_seconds"])
        self.assertEqual(Decimal(doubled["projection"]["scenarios"][0]["api_cost_usd"]),
                         2 * Decimal(original["projection"]["scenarios"][0]["api_cost_usd"]))

    def test_missing_memo_and_missing_usage_block_completion(self):
        self.calls = [call for call in self.calls if call["role"] != "memo"]
        self.calls[0]["input_tokens"] = None
        self.save()
        report = build_report(self.root)
        self.assertFalse(report["pilot_complete"])
        self.assertFalse(report["projection"]["available"])
        self.assertIsNone(report["actual"]["cold"]["total_api_cost_usd"])

    def test_missing_stage_times_does_not_invent_runtime(self):
        del self.metadata["stage_times_seconds"]
        self.save()
        report = build_report(self.root)
        self.assertTrue(report["projection"]["available"])
        self.assertIsNone(report["projection"]["scenarios"][0]["elapsed_seconds"])

    def test_warm_enrichment_and_missing_metadata_not_complete(self):
        self.calls.append(dict(self.calls[0], run_phase="warm"))
        self.save()
        report = build_report(self.root)
        self.assertFalse(report["actual"]["warm"]["complete"])
        (self.root / "cold.json").unlink()
        report = build_report(self.root)
        self.assertFalse(report["projection"]["available"])

    def test_active_runtime_is_not_wall_time(self):
        self.metadata.update(runtime_basis="active_attempts", active_elapsed_seconds=10,
                             wall_elapsed_seconds=110)
        self.save()
        report = build_report(self.root)
        actual = report["actual"]["cold"]
        self.assertEqual(actual["pause_seconds"], 100)
        self.assertEqual(actual["active_elapsed_seconds"], 10)
        self.assertEqual(report["projection"]["scenarios"][0]["runtime_basis"], "active_attempts")


if __name__ == "__main__":
    unittest.main()
