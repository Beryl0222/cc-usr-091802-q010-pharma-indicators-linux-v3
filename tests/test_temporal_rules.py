"""时间区间与有版本换算规则。"""

import unittest

from evidence_base.rules import RuleBook, RuleBookVersion, RuleGapError
from evidence_base.temporal import Interval, FXRate
from tests.helpers import rulebook


class IntervalTest(unittest.TestCase):
    def test_contains_and_open_ended(self):
        iv = Interval("2025-07-01", None)
        self.assertTrue(iv.contains("2025-12-31"))
        self.assertFalse(iv.contains("2025-06-30"))
        self.assertTrue(iv.is_open_ended)

    def test_intersection_clips_to_effective_window(self):
        a = Interval("2025-01-01", "2025-12-31")
        b = Interval("2025-07-01", None)
        cut = a.intersection(b)
        self.assertEqual(cut.start, "2025-07-01")
        self.assertEqual(cut.end, "2025-12-31")
        self.assertIsNone(Interval("2020-01-01", "2020-12-31").intersection(
            Interval("2025-01-01", None)))

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            Interval("2025-12-31", "2025-01-01")


class RuleBookTest(unittest.TestCase):
    def test_currency_conversion_pins_rate_and_version(self):
        rb = rulebook(usd=7.1).get("RB1")
        amount, info = rb.convert(1_000_000_000, "USD", "2025-12-31")
        self.assertAlmostEqual(amount, 7_100_000_000.0)
        self.assertEqual(info["rulebook_version"], "RB1")
        self.assertEqual(info["to"], "CNY")

    def test_uses_latest_rate_not_later_than_date(self):
        book = RuleBook()
        book.add_version(RuleBookVersion(
            "RB", "2026-01-01", fx_rates=[
                FXRate("2025-06-30", "USD", "CNY", 7.2),
                FXRate("2025-12-31", "USD", "CNY", 7.0),
            ]))
        rb = book.get("RB")
        amt_jun, _ = rb.convert(1, "USD", "2025-07-01")
        amt_dec, _ = rb.convert(1, "USD", "2025-12-31")
        self.assertAlmostEqual(amt_jun, 7.2)
        self.assertAlmostEqual(amt_dec, 7.0)

    def test_missing_fx_is_a_gap_not_a_guess(self):
        rb = rulebook().get("RB1")
        with self.assertRaises(RuleGapError):
            rb.convert(1, "GBP", "2025-12-31")

    def test_fiscal_calendar_normalizes_foreign_fiscal_year(self):
        book = rulebook(calendars={
            "P-KANSAI": {"FY2025JP": ("FY2025", "2025-04-01", "2026-03-31")}})
        fp = book.get("RB1").map_fiscal(
            "P-KANSAI", "FY2025JP", "2025-04-01", "2026-03-31")
        self.assertEqual(fp.period_key, "FY2025")

    def test_region_alias_normalization(self):
        rb = rulebook(regions={"US": ["USA", "United States", "美国"]}).get("RB1")
        self.assertEqual(rb.normalize_region("USA"), "US")
        self.assertEqual(rb.normalize_region("美国"), "US")

    def test_versioned_rules_latest_as_of(self):
        book = rulebook(version="RB1", usd=7.1, recorded="2026-01-01")
        book.add_version(RuleBookVersion(
            "RB2", "2027-01-01", fx_rates=[FXRate("2026-12-31", "USD", "CNY", 7.0)]))
        self.assertEqual(book.latest_as_of("2026-06-01").version, "RB1")
        self.assertEqual(book.latest_as_of("2027-06-01").version, "RB2")


if __name__ == "__main__":
    unittest.main()
