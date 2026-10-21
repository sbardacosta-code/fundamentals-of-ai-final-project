import json
import tempfile
import unittest
from pathlib import Path
from review_pipeline.ranking import rank


class RankingTests(unittest.TestCase):
    def test_ties_rounding_and_missing_membership(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);records=root/'records.jsonl';membership=root/'membership.csv'
            records.write_text('\n'.join(json.dumps(dict(review_id=str(i),status='completed',intent='complaint',severity=s)) for i,s in enumerate([1,1,2,4])))
            membership.write_text('issue_id,review_id\nb,0\nb,1\nb,2\na,3\n')
            rows=rank(records,membership,root/'ranking.csv')
            self.assertEqual([r['issue_id'] for r in rows],['a','b'])
            self.assertEqual(rows[1]['mean_severity'],'1.333333')
            membership.write_text('issue_id,review_id\na,3\n')
            with self.assertRaises(ValueError):rank(records,membership,root/'ranking.csv')
