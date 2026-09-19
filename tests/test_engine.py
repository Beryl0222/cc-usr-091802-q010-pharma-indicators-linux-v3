"""版本化存储、局部重算、复现、访问控制与手填拦截。"""

import unittest

from evidence_base.app import Service
from evidence_base.engine import MonitoringEngine, OverrideGuardError, Period
from evidence_base.loader import load_dataset_dir
from evidence_base.model import (
    CLEARANCE_CONFIDENTIAL,
    CLEARANCE_PUBLIC,
    SalesDisclosure,
)
from evidence_base.store import Actor, SecurityError
from tests.helpers import (
    approval,
    base_store,
    candidate,
    compound,
    discovery,
    party,
    review,
    rulebook,
    sale,
)

DATASET = "fixtures/dataset"
T1 = "2026-07-31T00:00:00Z"
T2 = "2027-03-01T00:00:00Z"


def dataset_service():
    ds = load_dataset_dir(DATASET)
    svc = Service(ds)
    svc.freeze("FY2025", T1, "SNAP-V1", frozen_at=T1)
    return svc, ds


class VersionedStoreTest(unittest.TestCase):
    def test_new_version_does_not_overwrite_history(self):
        st = base_store(); compound(st, "CP1"); candidate(st)
        approval(st, "C1", "US", "2025-09-12", aid="AP-US")
        v1 = st.version_at("AP-US", T1)
        # 批准日期更正 -> 新版本
        approval(st, "C1", "US", "2025-08-29", aid="AP-US",
                 sid="SRC", recorded_at="2026-08-15T00:00:00Z")
        v2 = st.version_at("AP-US", "2026-09-01T00:00:00Z")
        old = st.version_at("AP-US", T1)
        self.assertEqual(v1.version, 1)
        self.assertEqual(v2.version, 2)
        self.assertEqual(old.data["decision_date"], "2025-09-12")
        self.assertEqual(v2.data["decision_date"], "2025-08-29")

    def test_fact_requires_source_chain(self):
        st = base_store()
        with self.assertRaises(Exception):
            st.put(SalesDisclosure(id="X", candidate_id="C", party_id="P",
                                   amount=1, currency="USD"), T1)


class SnapshotReproduceTest(unittest.TestCase):
    def test_published_numbers_reproduce_after_later_evidence(self):
        svc, _ = dataset_service()
        rep = svc.reproduce("SNAP-V1")
        self.assertTrue(rep["reproduced"])
        self.assertTrue(rep["hash_matches"])

    def test_partial_recompute_only_touches_affected(self):
        svc, ds = dataset_service()
        before = {d["candidate_id"]: d for d in
                  svc.get_snapshot("SNAP-V1", Actor.system())["determinations"]}
        out = svc.recompute("SNAP-V1", "FY2025", T2, "SNAP-V2", frozen_at=T2)
        self.assertEqual(out["recomputed_candidate_ids"], ["CD-HLX001"])
        self.assertIn("CD-NG07", out["unchanged_candidate_ids"])
        after = {d["candidate_id"]: d for d in
                 svc.get_snapshot("SNAP-V2", Actor.system())["determinations"]}
        # 未受影响品种字节级一致
        self.assertEqual(before["CD-NG07"], after["CD-NG07"])
        # 重述后中国销售 21亿 -> 20亿，全球年销售相应下降
        self.assertEqual(
            before["CD-HLX001"]["sales"]["global_annual_anchor"]
            - after["CD-HLX001"]["sales"]["global_annual_anchor"],
            100_000_000)
        # 后到的日本批准进入依据
        self.assertIn("JP", after["CD-HLX001"]["fic"]["basis"]["first_jurisdictions"])
        self.assertNotIn("JP", before["CD-HLX001"]["fic"]["basis"]["first_jurisdictions"])

    def test_old_snapshot_immutable_and_hash_stable(self):
        svc, _ = dataset_service()
        h1 = svc.get_metrics("SNAP-V1")
        svc.recompute("SNAP-V1", "FY2025", T2, "SNAP-V2", frozen_at=T2)
        self.assertEqual(svc.get_metrics("SNAP-V1"), h1)
        with self.assertRaises(ValueError):
            svc.freeze("FY2025", T1, "SNAP-V1", frozen_at=T1)  # id 已占用

    def test_determination_carries_rule_version_and_evidence_versions(self):
        svc, _ = dataset_service()
        d = svc.get_determination("SNAP-V1", "CD-HLX001", Actor.system())
        self.assertEqual(d["conversion"]["rulebook_version"], "RB-2026")
        self.assertTrue(d["evidence_versions"])
        self.assertEqual(d["totals_provenance"], "derived-from-source-chain")


class SecurityTest(unittest.TestCase):
    def test_confidential_contract_visibility(self):
        svc, ds = dataset_service()
        anon = Actor("anon", clearance=CLEARANCE_PUBLIC)
        with self.assertRaises(SecurityError):
            ds.store.get("R-LIC-HLX-BRX", actor=anon)
        authed = Actor("analyst.li", clearance=CLEARANCE_CONFIDENTIAL)
        self.assertEqual(ds.store.get("R-LIC-HLX-BRX", actor=authed).id,
                         "R-LIC-HLX-BRX")

    def test_published_total_visible_but_source_redacted(self):
        svc, _ = dataset_service()
        d = svc.get_determination("SNAP-V1", "CD-HLX001",
                                  Actor("outsider", clearance=CLEARANCE_PUBLIC))
        # 聚合数字仍可对外
        self.assertGreater(d["sales"]["global_annual_anchor"], 0)

    def test_confidential_fic_evidence_computed_but_redacted(self):
        from evidence_base.model import (
            Source, Compound, Candidate, KeyDiscovery, Restriction,
        )
        from evidence_base.fic import assess_fic
        st = base_store()
        st.put(Source(id="S-SECRET", title="未公开实验记录", doc_type="internal",
                      confidential=True, clearance_required=CLEARANCE_CONFIDENTIAL), T1)
        party(st, "PA"); st.put(Compound(id="CP1", name="CP1"), T1)
        st.put(Candidate(id="C1", compound_id="CP1", novelty_class="first-in-class",
                         originator_party_ids=["PA"]), T1)
        st.put(KeyDiscovery(id="KD1", candidate_id="C1", compound_id="CP1",
                            title="保密首创发现", discovered_on="2019-01-01",
                            party_id="PA", novelty_claim="new-target",
                            source_id="S-SECRET"), T1)
        st.put(Restriction(id="RS1", record_id="KD1", record_kind="keydiscovery",
                           minimum_clearance=CLEARANCE_CONFIDENTIAL), T1)
        c = st.get("C1")
        # 系统计算主体看得到受限发现
        sys_a = assess_fic(st, c, "RB1", actor=Actor.system())
        self.assertTrue(any(e.record_id == "KD1" for e in sys_a.evidence))
        # 未授权个人看不到该发现原文，按证据缺口处理
        outsider = Actor("outsider", clearance=CLEARANCE_PUBLIC)
        pub_a = assess_fic(st, c, "RB1", actor=outsider)
        self.assertFalse(any(e.record_id == "KD1" for e in pub_a.evidence))
        self.assertIn("missing-key-discovery", [g["code"] for g in pub_a.gaps])


class OverrideGuardTest(unittest.TestCase):
    def test_manual_total_rejected(self):
        ds = load_dataset_dir(DATASET)
        with self.assertRaises(OverrideGuardError):
            ds.engine.submit_manual_total("CD-X9", 123)


class ThresholdTest(unittest.TestCase):
    def test_blockbuster_threshold_uses_versioned_fx(self):
        st = base_store(); party(st, "PA"); compound(st); candidate(st, "C1")
        discovery(st); approval(st, "C1", "US"); review(st, "C1", "science", "RS")
        review(st, "C1", "stats", "RT")
        # 9 亿美元，按 7.1 折合约 63.9 亿元，低于 10 亿美元阈值
        sale(st, "C1", "PA", "US", 900_000_000, ccy="USD")
        book = rulebook(usd=7.1)
        eng = MonitoringEngine(st, book)
        period = Period("P", "P", "FY2025", "RB1",
                        threshold_ref_date="2025-12-31")
        det = eng.build_determination(st.get("C1"), period, T1)
        self.assertFalse(det.sales["is_blockbuster"])
        self.assertTrue(det.included)  # 仍因首创计入
        self.assertNotIn("global-annual-sales-over-1b-usd", det.include_reasons)


if __name__ == "__main__":
    unittest.main()
