"""Synthetic fixtures, never actual experiments or human labels."""
import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from review_pipeline.optimization_report import build_report


class OptimizationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "baseline"
        self.base.mkdir()
        self.rows = [dict(review_id=str(i), topic="playback", intent="complaint", severity=4,
                          status="completed", sentiment=-0.5, needs_review=False) for i in range(100)]
        self.write(self.root / "reference.jsonl", self.rows)
        self.write(self.base / "pilot_records.jsonl", self.rows)
        self.write(self.base / "pilot_calls.jsonl", [])
        (self.base / "report.json").write_text(json.dumps({"projection": {"full_unique":484189,"scenarios":[{"name":"base_exact_reuse","stage_api_cost_usd":{"verify":"2","group":"3","memo":"0.01"}}]}}))
        self.write(self.root / "compact-b5.jsonl", self.rows)
        (self.root / "compact-b5-meta.json").write_text(json.dumps(dict(complete=True,count=100,unknown_cost_attempts=0,cost_usd=0.001)))

    def write(self, path, rows):
        path.write_text("".join(json.dumps(row)+"\n" for row in rows))

    def test_cost_and_provisional_gate(self):
        report=build_report(self.root,self.base)
        trial=report["candidates"]["compact-b5"]
        self.assertTrue(trial["provisional_gate"]["passes"])
        self.assertFalse(trial["provisional_gate"]["automatic_promotion"])
        self.assertEqual(Decimal(trial["projection"]["base_api_cost_usd"]),Decimal("9.85189"))
        self.assertEqual(trial["projection"]["base_stage_api_cost_usd"]["verify"],"2")
        self.assertNotIn("shared-b10", report["candidates"])

    def test_missing_ids_stay_in_denominator(self):
        self.write(self.root / "compact-b5.jsonl",self.rows[:50])
        trial=build_report(self.root,self.base)["candidates"]["compact-b5"]
        self.assertEqual(trial["metrics"]["primary_joint_accuracy"],0.5)
        self.assertEqual(trial["metrics"]["severe_recall"],0.5)
        self.assertFalse(trial["provisional_gate"]["passes"])

    def test_accepted_triples_do_not_mix_for_joint(self):
        refs=[dict(row,accepted_labels=[dict(topic="playback",intent="complaint",severity=4),dict(topic="other",intent="praise",severity=1)]) for row in self.rows]
        self.write(self.root / "reference.jsonl",refs)
        predictions=[dict(row,intent="praise") for row in self.rows]
        self.write(self.root / "compact-b5.jsonl",predictions)
        metrics=build_report(self.root,self.base)["candidates"]["compact-b5"]["metrics"]
        self.assertEqual(metrics["fields"]["intent"]["accepted"]["accuracy"],1)
        self.assertEqual(metrics["accepted_joint_accuracy"],0)

    def test_unknown_cost_not_zero(self):
        (self.root / "compact-b5-meta.json").write_text(json.dumps(dict(complete=True,count=100,unknown_cost_attempts=1,cost_usd=0)))
        self.assertFalse(build_report(self.root,self.base)["candidates"]["compact-b5"]["projection"]["available"])

    def test_projection_uses_99_measured_unique_enrichments(self):
        meta_path=self.root / "compact-b5-meta.json"
        metadata=json.loads(meta_path.read_text())
        metadata["unique_enrichment_count"]=99
        meta_path.write_text(json.dumps(metadata))
        projection=build_report(self.root,self.base)["candidates"]["compact-b5"]["projection"]
        expected=Decimal("0.001") / 99 * 484189
        self.assertEqual(Decimal(projection["base_stage_api_cost_usd"]["enrich"]),expected)
        self.assertEqual(projection["unique_enrichment_count"],99)
        self.assertFalse(projection["unique_enrichment_count_assumed"])
        del metadata["unique_enrichment_count"]
        meta_path.write_text(json.dumps(metadata))
        legacy=build_report(self.root,self.base)["candidates"]["compact-b5"]["projection"]
        self.assertTrue(legacy["unique_enrichment_count_assumed"])
        self.assertIn("assumed, not measured",legacy["assumptions"])

    def test_dynamic_trials_and_holdout_baseline(self):
        self.write(self.root / "baseline-b5.jsonl",self.rows)
        metadata=(self.root / "compact-b5-meta.json").read_text()
        for name in ("shared_short-b20", "shared_rules-b10", "shared-warm-b5", "cache-b5", "baseline-b5"):
            (self.root / (name+"-meta.json")).write_text(metadata)
            self.write(self.root / (name+".jsonl"),[dict(row,severity=1) for row in self.rows] if name=="shared_short-b20" else self.rows)
        report=build_report(self.root,self.base,self.root / "baseline-b5.jsonl")
        self.assertIn("shared_rules-b10",report["candidates"])
        self.assertNotIn("shared-warm-b5",report["candidates"])
        self.assertNotIn("cache-b5",report["candidates"])
        self.assertNotIn("baseline-b5",report["candidates"])
        gate=report["candidates"]["shared_short-b20"]["provisional_gate"]
        self.assertFalse(gate["passes"])
        self.assertFalse(gate["checks"]["severe_recall"])
        self.assertIn("| baseline |",(self.root / "report.md").read_text())

    def test_primary_accuracy_gate_rejects_accepted_alternative(self):
        refs=[dict(row,accepted_labels=[dict(topic="playback",intent="complaint",severity=4),dict(topic="other",intent="complaint",severity=4)]) for row in self.rows]
        self.write(self.root / "reference.jsonl",refs)
        self.write(self.root / "compact-b5.jsonl",[dict(row,topic="other") for row in self.rows])
        gate=build_report(self.root,self.base)["candidates"]["compact-b5"]["provisional_gate"]
        self.assertTrue(gate["checks"]["topic"])
        self.assertFalse(gate["checks"]["primary_topic"])
        self.assertFalse(gate["passes"])


if __name__ == "__main__":
    unittest.main()
