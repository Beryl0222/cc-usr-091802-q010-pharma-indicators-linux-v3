"""销售合并：去重、内部抵销、权利金、代收份额、币种财年归一、冲突挂缺口。"""

import unittest

from evidence_base.consolidation import consolidate_sales
from evidence_base.model import (
    REL_ACQUISITION,
    REL_CODEVELOPMENT,
    REL_LICENSE,
)
from tests.helpers import (
    base_store,
    candidate,
    compound,
    party,
    relation,
    rulebook,
    sale,
)


def cand(store, cid="C1"):
    compound(store, "CP1")
    return candidate(store, cid)


class DedupAndRightsTest(unittest.TestCase):
    def test_license_grantor_royalty_not_double_counted(self):
        st = base_store()
        party(st, "PA"); party(st, "PB")
        c = cand(st)
        relation(st, c.id, REL_LICENSE, "PA", "PB", ["US"], start="2022-01-01")
        sale(st, c.id, "PA", "US", 180_000_000, ccy="USD", kind="royalty")
        sale(st, c.id, "PB", "US", 1_300_000_000, ccy="USD")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        self.assertEqual(res.global_sales_anchor, 1_300_000_000 * 7.1)
        royalty = next(l for l in res.lines if "royalty-after-rights-transfer" in l.reasons)
        self.assertEqual(royalty.anchor_amount_included, 0)

    def test_currency_normalization_to_anchor(self):
        st = base_store(); party(st, "PA"); c = cand(st)
        sale(st, c.id, "PA", "US", 1_000_000_000, ccy="USD")
        res = consolidate_sales(st, c, rulebook(usd=7.1).get("RB1"))
        self.assertEqual(res.global_sales_anchor, 7_100_000_000.0)
        self.assertEqual(res.by_region["US"], 7_100_000_000.0)

    def test_partner_share_removed(self):
        st = base_store(); party(st, "PA"); party(st, "PB"); c = cand(st)
        sale(st, c.id, "PA", "US", 600_000_000, ccy="USD",
             includes_partner_share=True, partner_party_id="PB",
             partner_share_amount=250_000_000)
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        self.assertEqual(res.global_sales_anchor, 350_000_000 * 7.1)

    def test_distinct_regions_both_count(self):
        st = base_store(); party(st, "PA"); party(st, "PB"); c = cand(st)
        sale(st, c.id, "PA", "CN", 2_000_000_000, ccy="CNY")
        sale(st, c.id, "PB", "US", 1_000_000_000, ccy="USD")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        self.assertEqual(res.global_sales_anchor, 2_000_000_000 + 7_100_000_000)


class IntraGroupEliminationTest(unittest.TestCase):
    def test_static_group_intercompany_eliminated(self):
        st = base_store()
        party(st, "HOLD", group="G1"); party(st, "SUB", group="G1")
        c = cand(st)
        sale(st, c.id, "SUB", "US", 200_000_000, ccy="USD",
             counterparty_party_id="HOLD")
        sale(st, c.id, "HOLD", "US", 900_000_000, ccy="USD")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        # 仅 HOLD 对外销售计入
        self.assertEqual(res.global_sales_anchor, 900_000_000 * 7.1)
        self.assertEqual(len(res.eliminations), 1)

    def test_acquisition_only_affects_post_effective_window(self):
        st = base_store()
        party(st, "GIANT", group="GG"); party(st, "TARGET", group="GT")
        c = cand(st)
        relation(st, c.id, REL_ACQUISITION, "GIANT", "TARGET", [],
                 start="2025-07-01")
        # 生效日前（H1）：尚不同集团 -> 计入
        sale(st, c.id, "TARGET", "US", 500_000_000, ccy="USD",
             start="2025-01-01", end="2025-06-30",
             counterparty_party_id="GIANT", sid_id="SL-H1")
        # 生效日后（H2）：同集团 -> 抵销
        sale(st, c.id, "GIANT", "US", 300_000_000, ccy="USD",
             start="2025-07-01", end="2025-12-31",
             counterparty_party_id="TARGET", sid_id="SL-H2")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        ids = {e["disclosure_id"] for e in res.eliminations}
        self.assertEqual(ids, {"SL-H2"})
        self.assertEqual(res.global_sales_anchor, 500_000_000 * 7.1)


class ConflictTest(unittest.TestCase):
    def test_codevelopment_double_book_is_parked(self):
        st = base_store(); party(st, "PA"); party(st, "PB"); c = cand(st)
        relation(st, c.id, REL_CODEVELOPMENT, "PA", "PB", ["US"], start="2020-01-01")
        sale(st, c.id, "PA", "US", 600_000_000, ccy="USD")
        sale(st, c.id, "PB", "US", 300_000_000, ccy="USD")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        self.assertEqual(res.global_sales_anchor, 0)
        self.assertTrue(any(
            cf["type"] == "codevelopment-double-book" for cf in res.conflicts))
        self.assertTrue(any(g["kind"] == "attribution-conflict" for g in res.gaps))
        for line in res.lines:
            self.assertIn("unresolved-attribution-conflict", line.reasons)

    def test_unrelated_multiparty_sales_conflict(self):
        st = base_store(); party(st, "PA"); party(st, "PB"); c = cand(st)
            # 无任何关系链
        sale(st, c.id, "PA", "US", 600_000_000, ccy="USD")
        sale(st, c.id, "PB", "US", 400_000_000, ccy="USD")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        self.assertEqual(res.global_sales_anchor, 0)
        self.assertTrue(any(
            cf["type"] == "unexplained-multiparty-booked-sales"
            for cf in res.conflicts))


class RestatementTest(unittest.TestCase):
    def test_restated_disclosure_superseded(self):
        st = base_store(); party(st, "PA"); c = cand(st)
        sale(st, c.id, "PA", "US", 900_000_000, ccy="USD", sid_id="SL-OLD")
        sale(st, c.id, "PA", "US", 800_000_000, ccy="USD",
             sid_id="SL-NEW", restatement_of="SL-OLD")
        res = consolidate_sales(st, c, rulebook().get("RB1"))
        self.assertEqual(res.global_sales_anchor, 800_000_000 * 7.1)
        old = next(l for l in res.lines if l.disclosure_id == "SL-OLD")
        self.assertIn("restated-superseded", old.reasons)


if __name__ == "__main__":
    unittest.main()
