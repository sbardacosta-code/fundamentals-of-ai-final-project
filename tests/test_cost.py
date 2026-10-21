"""Synthetic unit fixtures only: these are not measured pilot evidence."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from review_pipeline.cost import RATES_TEMPLATE, replay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.calls = self.root / "calls.jsonl"
        self.rates = self.root / "rates.csv"
        self.output = self.root / "report.json"
        self.write_rates()

    def write_rates(self, factor=1):
        with self.rates.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(RATES_TEMPLATE.strip().split(","))
            writer.writerow(["synthetic-test-model", 2 * factor, 8 * factor, factor,
                             "https://example.invalid/unit-test", "synthetic-fixture"])

    def call(self, **overrides):
        result = dict(request_id="test-1", role="enrich", model="synthetic-test-model",
                      input_tokens=1000, cached_input_tokens=400, output_tokens=100,
                      outcome="succeeded", run_id="fixture", run_phase="cold",
                      started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:10Z")
        result.update(overrides)
        return result

    def run_replay(self, calls, volume=660622):
        self.calls.write_text("".join(json.dumps(call) + "\n" for call in calls))
        return replay(self.calls, self.rates, self.output, volume)

    def test_rates_and_volume_do_not_change_measured_time(self):
        calls = [self.call(cost_usd="0.0024")]
        first = self.run_replay(calls)
        self.assertEqual(first["actual"]["calculated_api_cost_known_subtotal_usd"], "0.0024")
        self.write_rates(2)
        second = self.run_replay(calls)
        self.assertEqual(second["actual"]["calculated_api_cost_known_subtotal_usd"], "0.0048")
        self.assertEqual(first["actual"]["call_window_seconds"], second["actual"]["call_window_seconds"])
        self.assertEqual(first["actual"]["local_compute_cost_usd"], second["actual"]["local_compute_cost_usd"])
        self.assertEqual(first["actual"]["reported_cost_known_subtotal_usd"], second["actual"]["reported_cost_known_subtotal_usd"])
        third = self.run_replay(calls, volume=100000)
        self.assertEqual(second["actual"], third["actual"])
        self.assertFalse(third["projection"]["available"])
        self.assertEqual(json.loads(self.output.read_text()), third)

    def test_failures_and_unknown_usage_are_not_free(self):
        report = self.run_replay([
            self.call(outcome="failed"),
            self.call(request_id="test-2", input_tokens=None, cost_usd="0.02"),
            self.call(request_id="test-3", model="unpriced"),
            self.call(request_id="test-4", cached_input_tokens=1001),
        ])
        actual = report["actual"]
        self.assertEqual(actual["failed_attempts"], 1)
        self.assertEqual(actual["priced_attempts"], 1)
        self.assertEqual(actual["unknown_cost_attempts"], 3)
        self.assertFalse(actual["api_cost_complete"])
        self.assertEqual(actual["calculated_api_cost_known_subtotal_usd"], "0.0024")

    def test_overlapping_calls_and_phase_grouping(self):
        report = self.run_replay([self.call(), self.call(request_id="test-2", role="verify",
            run_phase="warm", started_at="2026-01-01T00:00:05Z", ended_at="2026-01-01T00:00:15Z")])
        self.assertEqual(report["actual"]["call_window_seconds"], 15)
        self.assertEqual(report["actual"]["sum_request_seconds"], 20)
        self.assertIsNone(report["actual"]["end_to_end_wall_seconds"])
        self.assertEqual(len(report["groups"]), 2)

    def test_empty_log_is_not_successful_warm_evidence(self):
        report = self.run_replay([])
        self.assertFalse(report["actual"]["api_cost_complete"])
        self.assertIsNone(report["actual"]["call_window_seconds"])

    def test_duplicate_attempt_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate request"):
            self.run_replay([self.call(), self.call()])

    def test_missing_cached_usage_and_nonfinite_usage(self):
        for bad in (None, "NaN", -1, True, 0.5):
            with self.subTest(bad=bad):
                result = self.run_replay([self.call(cached_input_tokens=bad)])
                self.assertEqual(result["actual"]["unknown_cost_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
