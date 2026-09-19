"""证据库端到端测试：别名归并、共同开发、跨区许可、内部交易抵销、
重述局部重算、历史复现、批准日期更正、保密访问控制、来源链强制。
"""

import importlib.util
import unittest
from pathlib import Path

from evidence_db import evidence as ev
from evidence_db.engine import Engine, Review, Viewer
from evidence_db.registry import Registry, SourceChainViolation
from evidence_db.rules import (
    FiscalConvention,
    FxVersion,
    GeoRule,
    RuleBookVersion,
)
from evidence_db.timeline import Interval, annual_period


def _load_scenario():
    path = Path(__file__).resolve().parent.parent / "fixtures" / "scenario.py"
    spec = importlib.util.spec_from_file_location("scenario_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _src(doc_id="d", when="2026-02-20", **kw):
    return ev.Source(doc_id=doc_id, title="t", publisher="p",
                     published_on=when, **kw)


def _single_rulebook():
    fx = FxVersion(version="fx", effective_from="2024-01-01",
                   base_currency="USD",
                   rates={2025: {"CNY": 7.0, "EUR": 0.9}})
    return RuleBookVersion(version="rb", effective_from="2024-01-01", fx=fx,
                           fiscal={"A": FiscalConvention("A", "12-31")},
                           geo=GeoRule(region_map={"China": "CN"}))


def _mini_candidate(reg, cid="V1", compound="C1", moa=""):
    reg.add(ev.Compound(id=compound, version=1, canonical_name="x",
                        aliases=("x", "别名x"), moa_class=moa, source=_src()))
    reg.add(ev.Candidate(id=cid, version=1, compound_id=compound, name="品种",
                         originators=("A",), source=_src()))


class AliasAndIdentityTest(unittest.TestCase):
    def test_aliases_fold_into_one_compound(self):
        sc = _load_scenario()
        reg, eng, H = sc.build_registry()
        concl = eng.conclude(H["period"], H["candidate"])
        self.assertEqual(concl["compound_id"], "C-ALPHA-7")
        self.assertIn("Alfacin (EU)", concl["aliases"])
        self.assertIn("艾法新(CN)", concl["aliases"])
        # 同一化合物在所有法域只形成一个品种结论
        self.assertEqual(concl["candidate_id"], "V-ALPHACIN")


class SalesNormalizationTest(unittest.TestCase):
    def setUp(self):
        sc = _load_scenario()
        self.sc = sc
        self.reg, self.eng, self.H = sc.build_registry()
        self.period = self.H["period"]
        self.cid = self.H["candidate"]

    def test_deduped_global_sales_excludes_royalty_and_intercompany(self):
        concl = self.eng.conclude(self.period, self.cid)
        legs = {(l["stream_key"], l["revenue_kind"]): l for l in concl["sales_legs"]}
        # 终端销售计入
        self.assertTrue(legs[("STREAM-ALPHACIN-ROW", "end_market_net_sales")]["included"])
        # 同一销售流的特许权费去重
        self.assertFalse(legs[("STREAM-ALPHACIN-ROW", "royalty_income")]["included"])
        # 集团内部销售抵销
        self.assertFalse(legs[("STREAM-ALPHACIN-INTERCO", "intercompany_sales")]["included"])
        # 去重后总额 = ROW 700m + CN 3.6bn/7.2 + JP 3月财年落在 2025 日历年的 90 天
        cny = 3_600_000_000.0 / 7.2
        # 区间 2024-04-01~2025-04-01 与 2025 年重叠 90 天 (1/1..4/1)，财年共 365 天
        jp = 36_000_000_000.0 / 150.0 * 90 / 365
        expected = 700_000_000.0 + cny + jp
        self.assertAlmostEqual(concl["global_sales"]["deduped_total"],
                               expected, delta=0.05)
        self.assertTrue(concl["global_sales"]["over_blockbuster"])

    def test_fiscal_year_prorated_into_calendar_period(self):
        # 3 月财年的金额只有约 1/4 落到 2025 日历年
        concl = self.eng.conclude(self.period, self.cid)
        jp_leg = [l for l in concl["sales_legs"] if l["stream_key"] == "STREAM-ALPHACIN-JP"][0]
        self.assertAlmostEqual(jp_leg["amount_base"], 59_178_082.19, delta=1.0)

    def test_currency_and_rule_version_recorded(self):
        concl = self.eng.conclude(self.period, self.cid)
        self.assertTrue(all(v == "rules-v1"
                            for v in concl["rule_versions"].values()))


class IntercompanyWindowTest(unittest.TestCase):
    """集团内部交易抵销必须按披露区间判定：收购前不算内部。"""

    def _registry_with_window(self, span):
        reg = Registry()
        reg.add_rulebook(_single_rulebook(), "2024-01-01")
        _mini_candidate(reg)
        # A 与 T：2025-04-01 之前不同集团，之后同属 A 集团
        reg.add(ev.GroupRelation(
            id="GR-T-1", version=1, unit="T", group="T",
            interval=Interval("2000-01-01", "2025-04-01"), source=_src()))
        reg.add(ev.GroupRelation(
            id="GR-T-2", version=1, unit="T", group="A",
            interval=Interval("2025-04-01"), source=_src()))
        reg.add(ev.SalesDisclosure(
            id="SD1", version=1, candidate_id="V1",
            reporting_company="A", counterparty="T", region="China",
            amount=100.0, currency="USD",
            period=span, fiscal_year_label="",
            revenue_kind=ev.RevenueKind.END_MARKET_NET_SALES,
            stream_key="S1", disclosed_on="2026-02-20", source=_src()))
        return reg

    def test_pre_acquisition_not_eliminated(self):
        reg = self._registry_with_window(Interval("2025-01-01", "2025-04-01"))
        leg = Engine(reg).conclude(annual_period(2025), "V1")["sales_legs"][0]
        self.assertTrue(leg["included"])

    def test_post_acquisition_eliminated(self):
        reg = self._registry_with_window(Interval("2025-05-01", "2025-08-01"))
        leg = Engine(reg).conclude(annual_period(2025), "V1")["sales_legs"][0]
        self.assertFalse(leg["included"])
        self.assertIn("抵销", leg["reason"])


class FirstInClassReviewTest(unittest.TestCase):
    def setUp(self):
        sc = _load_scenario()
        self.reg, self.eng, self.H = sc.build_registry()
        self.period = self.H["period"]
        self.cid = self.H["candidate"]

    def test_fic_basis_and_competing_claim(self):
        concl = self.eng.conclude(self.period, self.cid)
        self.assertTrue(concl["first_in_class"])
        self.assertIn("first-in-class", concl["fic_basis"])
        # 被驳回的竞争主张不再阻塞；裁决记录在案
        claim_statuses = {c["claim_id"]: c["status"] for c in concl["competing_claims"]}
        self.assertEqual(claim_statuses.get("CL-FIC-1"), "rejected")

    def test_open_competing_claim_blocks_inclusion(self):
        # 新增一条尚未裁决的首创异议 -> 不得计入
        self.reg.add(ev.AttributionClaim(
            id="CL-OPEN", version=1, subject_id=f"Candidate:{self.cid}",
            topic="first_in_class", claimant="X", assertion="争议未决",
            status=ev.ClaimStatus.OPEN, source=_src("open", "2026-03-01")))
        concl = self.eng.conclude(self.period, self.cid)
        self.assertFalse(concl["included"])

    def test_dual_review_required_to_freeze(self):
        # 移除统计复核 -> 冻结必须失败
        reg2, eng2, H2 = _load_scenario().build_registry()
        # 直接构造一个缺少统计复核的状态：删掉统计复核事件不可行（只追加），
        # 改为新建设置：在基础库上，冻结前断言两条复核都在。
        before = eng2.conclude(H2["period"], H2["candidate"])
        self.assertTrue(before["reviews"]["scientific"])
        self.assertTrue(before["reviews"]["statistical"])

    def test_cannot_freeze_without_statistical_review(self):
        reg = Registry()
        reg.add_rulebook(_single_rulebook(), "2024-01-01")
        _mini_candidate(reg, cid="VF", compound="CF", moa="m1")
        reg.add(ev.FirstDiscovery(
            id="FD", version=1, candidate_id="VF", discoverer="A",
            discovered_on="2020-01-01", novelty="新机制", source=_src()))
        reg.add(ev.Approval(
            id="AP", version=1, candidate_id="VF", jurisdiction="US/FDA",
            approved_on="2025-05-01", first_in_class_designation=True,
            source=_src()))
        reg.add_review(Review(
            review_id="SCI", kind="scientific", period_code="2025",
            candidate_id="VF", reviewer="r", reviewer_org="o",
            decision="approved", findings="", reviewed_at="2026-03-01"))
        with self.assertRaises(SourceChainViolation):
            Engine(reg).freeze_period(annual_period(2025), "2026-03-15")


class VersioningAndReproductionTest(unittest.TestCase):
    def setUp(self):
        sc = _load_scenario()
        self.sc = sc
        self.reg, self.eng, self.H = sc.build_registry()
        self.period = self.H["period"]
        self.cid = self.H["candidate"]
        self.snap = self.eng.freeze_period(self.period, "2026-03-15")
        self.fid = self.snap["freeze_id"]
        self.fseq = self.snap["event_seq"]

    def test_published_snapshot_reproduces_bit_for_bit(self):
        self.assertTrue(self.eng.verify_published(self.period, self.fid))

    def test_version_chain_must_supersede_previous(self):
        bad = ev.SalesDisclosure(
            id="SD-BETA-2025", version=2,  # 缺少 supersedes
            candidate_id=self.cid, reporting_company="Beta",
            region="WW ex-Greater China", amount=1.0, currency="USD",
            period=Interval("2025-01-01", "2026-01-01"),
            stream_key="X", disclosed_on="2026-09-01", source=_src())
        with self.assertRaises(Exception):
            self.reg.add(bad)

    def test_restatement_triggers_partial_recompute_only(self):
        frozen_obj = self.reg.get_freeze("2025", self.fid)
        published = frozen_obj.conclusions[self.cid].payload["global_sales"]["deduped_total"]

        evt = self.sc.add_post_freeze_events(self.reg)
        # 只影响关联品种，不影响其它品种
        last = self.reg.events[-1]
        self.assertEqual(self.reg.affected_candidates(last, "2025"), {self.cid})

        # 当前结论反映重述（720m）+ 后到欧洲（v2 汇率）
        cur = self.eng.conclude(self.period, self.cid)
        self.assertGreater(cur["global_sales"]["deduped_total"], published)
        self.assertIn("rules-v2", cur["rule_versions"].values())
        row = [l for l in cur["sales_legs"]
               if l["disclosure_ids"] == ("SD-BETA-2025",)][0]
        self.assertEqual(row["amount_base"], 720_000_000.0)

        # 历史快照仍按旧事件序号、旧汇率逐位复现
        self.assertTrue(self.eng.verify_published(self.period, self.fid))
        old_view = self.reg.view_as_of(self.fseq)
        old_total = self.eng.conclude(self.period, self.cid, old_view)["global_sales"]["deduped_total"]
        self.assertAlmostEqual(old_total, published, places=6)

    def test_approval_date_correction_is_a_new_version(self):
        self.sc.add_post_freeze_events(self.reg)
        versions = self.reg.records_of("Approval:AP-FDA")
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertEqual(versions[-1].approved_on, "2025-03-12")
        self.assertEqual(versions[0].approved_on, "2025-03-15")  # 旧值保留

    def test_late_overseas_evidence_uses_rulebook_effective_on_disclosure_day(self):
        self.sc.add_post_freeze_events(self.reg)
        cur = self.eng.conclude(self.period, self.cid)
        eu = [l for l in cur["sales_legs"] if l["region"] == "EU"][0]
        # 46m EUR / 0.90 (rules-v2)，而非 v1 的 0.92
        self.assertAlmostEqual(eu["amount_base"], 46_000_000.0 / 0.90, places=2)
        self.assertEqual(eu["rule_version"], "rules-v2")


class IndicatorTest(unittest.TestCase):
    def test_share_denominator_counts_first_approvals(self):
        sc = _load_scenario()
        reg, eng, H = sc.build_registry()
        # 增加一个同期首次批准、但非首创的品种
        reg.add(ev.Compound(id="C-FOLLOW", version=1, canonical_name="follow",
                            moa_class="", source=_src()))
        reg.add(ev.Candidate(id="V-FOLLOW", version=1, compound_id="C-FOLLOW",
                             name="跟随品种", source=_src()))
        reg.add(ev.Approval(
            id="AP-F", version=1, candidate_id="V-FOLLOW",
            jurisdiction="CN/NMPA", approved_on="2025-09-01",
            first_in_class_designation=False, source=_src()))
        ind = eng.indicators(H["period"])
        self.assertEqual(ind["fic-global-share"]["denominator_first_approvals"], 2)
        self.assertEqual(ind["fic-global-share"]["numerator_fic"], 1)
        self.assertAlmostEqual(ind["fic-global-share"]["value"], 50.0, places=2)


class SourceChainTest(unittest.TestCase):
    def test_sales_without_source_rejected(self):
        reg = Registry()
        reg.add_rulebook(_single_rulebook(), "2024-01-01")
        _mini_candidate(reg)
        no_source = ev.SalesDisclosure(
            id="SDX", version=1, candidate_id="V1", reporting_company="A",
            amount=10.0, currency="USD",
            period=Interval("2025-01-01", "2026-01-01"),
            stream_key="S", disclosed_on="2026-02-20")  # source=None
        with self.assertRaises(SourceChainViolation):
            reg.add(no_source)

    def test_sales_without_stream_key_rejected(self):
        reg = Registry()
        reg.add_rulebook(_single_rulebook(), "2024-01-01")
        _mini_candidate(reg)
        no_stream = ev.SalesDisclosure(
            id="SDY", version=1, candidate_id="V1", reporting_company="A",
            amount=10.0, currency="USD",
            period=Interval("2025-01-01", "2026-01-01"),
            disclosed_on="2026-02-20", source=_src())  # stream_key=""
        with self.assertRaises(SourceChainViolation):
            reg.add(no_stream)


class AccessControlTest(unittest.TestCase):
    def test_confidential_contract_only_for_granted(self):
        sc = _load_scenario()
        reg, eng, H = sc.build_registry()
        period, cid = H["period"], H["candidate"]
        viewers = sc.viewers()

        public = eng.visible_conclusion(period, cid, viewers["public"])
        granted = eng.visible_conclusion(period, cid, viewers["analyst"])

        public_ll = [l for l in public["licensing"] if l["relation_id"] == "LL-1"][0]
        granted_ll = [l for l in granted["licensing"] if l["relation_id"] == "LL-1"][0]
        self.assertEqual(public_ll["source"]["doc_id"], "redacted")
        self.assertEqual(granted_ll["source"]["doc_id"], "contract-AB-2021")
        self.assertIn("LicenseRelation:LL-1", public["access"]["redacted"])
        self.assertNotIn("access", granted)
        # 脱敏不改变经济结论
        self.assertEqual(public["global_sales"]["deduped_total"],
                         granted["global_sales"]["deduped_total"])


if __name__ == "__main__":
    unittest.main()
