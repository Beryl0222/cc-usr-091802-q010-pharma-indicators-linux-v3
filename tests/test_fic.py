"""首创认定：竞争性意见、缺口、科学/统计双复核、冻结资格。"""

import unittest

from evidence_base.fic import assess_fic
from tests.helpers import (
    approval,
    base_store,
    candidate,
    compound,
    discovery,
    opinion,
    party,
    review,
)


def _ready(st, cid="C1", originator="PA"):
    party(st, originator)
    compound(st, "CP1")
    candidate(st, cid, originators=(originator,))
    discovery(st, cid, party_id=originator)
    approval(st, cid, "US")
    review(st, cid, "science", "RV-SCI")
    review(st, cid, "stats", "RV-STAT")


class FICWorkflowTest(unittest.TestCase):
    def test_ready_after_both_reviews(self):
        st = base_store(); _ready(st)
        c = st.get("C1")
        a = assess_fic(st, c, "RB1")
        self.assertEqual(a.status, "ready-to-freeze")
        self.assertTrue(a.counts_as_fic)
        self.assertEqual(a.gaps, [])

    def test_missing_stats_review_blocks(self):
        st = base_store(); party(st, "PA"); compound(st, "CP1"); candidate(st)
        discovery(st); approval(st, "C1", "US"); review(st, "C1", "science", "RV-SCI")
        a = assess_fic(st, st.get("C1"), "RB1")
        self.assertEqual(a.status, "pending-review")
        self.assertFalse(a.counts_as_fic)
        self.assertIn("missing-stats-review", [g["code"] for g in a.gaps])

    def test_no_discovery_is_insufficient(self):
        st = base_store(); party(st, "PA"); compound(st, "CP1"); candidate(st)
        approval(st, "C1", "US"); review(st, "C1", "science", "RV-SCI")
        review(st, "C1", "stats", "RV-STAT")
        a = assess_fic(st, st.get("C1"), "RB1")
        self.assertEqual(a.status, "insufficient-evidence")

    def test_competing_opinions_contested_until_adopted(self):
        st = base_store(); _ready(st)
        opinion(st, "C1", "OP-A", fic=True, originator_party_id="PA")
        opinion(st, "C1", "OP-B", fic=True, originator_party_id="PB")
        c = st.get("C1")
        contested = assess_fic(st, c, "RB1")
        self.assertEqual(contested.status, "contested")
        self.assertEqual(len(contested.competing_opinions), 2)
        # 复核须针对被采纳意见：为 OP-B 补双复核后才可冻结
        review(st, "C1", "science", "RV-SCI-B", opinion_id="OP-B")
        review(st, "C1", "stats", "RV-STAT-B", opinion_id="OP-B")
        adopted = assess_fic(st, c, "RB1", adopted_opinion_id="OP-B")
        self.assertEqual(adopted.status, "ready-to-freeze")
        self.assertTrue(adopted.counts_as_fic)
        self.assertEqual(adopted.adopted_opinion_id, "OP-B")

    def test_adopted_not_fic_opinion_excludes(self):
        st = base_store(); _ready(st)
        opinion(st, "C1", "OP-NO", fic=False)
        review(st, "C1", "science", "RV-SCI-N", opinion_id="OP-NO")
        review(st, "C1", "stats", "RV-STAT-N", opinion_id="OP-NO")
        a = assess_fic(st, st.get("C1"), "RB1", adopted_opinion_id="OP-NO")
        self.assertEqual(a.status, "not-first-in-class")
        self.assertFalse(a.counts_as_fic)


if __name__ == "__main__":
    unittest.main()
