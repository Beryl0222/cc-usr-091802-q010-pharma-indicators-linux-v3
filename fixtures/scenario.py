"""端到端示例场景（不含真实企业数据，企业与品种均为虚构）。

场景时间线
==========
* 化合物 ``C-ALPHA-7`` 在中/美/欧使用不同别名；品种 ``V-ALPHACIN`` 由
  Alpha 发起、与 Beta 共同开发，Beta 获大中华区以外的跨区许可。
* Alpha 于 2025-04 收购 Gamma；收购后的集团内部销售在汇总时抵销，
  收购前的独立交易区间保持有效。
* 2025 年取得 FDA（含 first-in-class  designation）与 EMA 批准；存在一条
  关于首创归属的竞争主张，被科学复核驳回。
* 销售披露涵盖：Beta 终端销售（美元）、Alpha 中国区（人民币）、日本合作方
  3 月结日财年（日元，按天分摊回 2025 日历年）、Beta 支付的特许权费
  （与终端销售同一销售流，去重）、Alpha→Gamma 内部销售（抵销）。
* 2025 期经科学与统计双重复核后冻结；此后 Beta 重述年报（新版本）、
  FDA 更正批准日期（新版本）、后到的欧洲证据在新版换算规则下到达——
  只触发该品种的局部重算，已发布快照仍逐位可复现。
"""

from __future__ import annotations

from evidence_db import evidence as ev
from evidence_db.engine import Engine, Review, Viewer
from evidence_db.registry import Registry
from evidence_db.rules import (
    FiscalConvention,
    FxVersion,
    GeoRule,
    RuleBookVersion,
)
from evidence_db.timeline import Interval, annual_period

PERIOD = annual_period(2025)


def _src(doc_id, title, publisher, published_on, **kw):
    return ev.Source(doc_id=doc_id, title=title, publisher=publisher,
                     published_on=published_on, **kw)


def build_registry() -> tuple[Registry, Engine, dict]:
    reg = Registry()

    # ----- 换算规则（两个版本） -----------------------------------------
    fx_v1 = FxVersion(
        version="fx-2024",
        effective_from="2024-01-01",
        base_currency="USD",
        rates={
            2024: {"CNY": 7.20, "JPY": 150.0, "EUR": 0.92},
            2025: {"CNY": 7.20, "JPY": 150.0, "EUR": 0.92},
        },
    )
    book_v1 = RuleBookVersion(
        version="rules-v1",
        effective_from="2024-01-01",
        fx=fx_v1,
        fiscal={
            "Alpha": FiscalConvention("Alpha", "12-31"),
            "Beta": FiscalConvention("Beta", "12-31"),
            "NihonPharma": FiscalConvention("NihonPharma", "03-31"),
        },
        geo=GeoRule(region_map={
            "Greater China": "CN",
            "WW ex-Greater China": "ROW",
            "Japan": "JP",
            "Europe": "EU",
        }),
        note="首版口径：年均汇率、日历年/3月财年、地域四分区",
    )
    fx_v2 = FxVersion(
        version="fx-2026h1",
        effective_from="2026-06-01",
        base_currency="USD",
        rates={
            2025: {"CNY": 7.00, "JPY": 145.0, "EUR": 0.90},
            2026: {"CNY": 7.00, "JPY": 145.0, "EUR": 0.90},
        },
    )
    book_v2 = RuleBookVersion(
        version="rules-v2",
        effective_from="2026-06-01",
        fx=fx_v2,
        fiscal=dict(book_v1.fiscal),
        geo=book_v1.geo,
        note="修订：2025 年参考汇率与地域说明更新（不回溯历史发布）",
    )
    reg.add_rulebook(book_v1, "2024-01-01")
    reg.add_rulebook(book_v2, "2026-06-01")

    # ----- 化合物 / 品种 / 适应证 ----------------------------------------
    compound = ev.Compound(
        id="C-ALPHA-7", version=1,
        canonical_name="alphacinib",
        aliases=("alphacinib", "阿法西尼", "ALPHA-7", "Alfacin (EU)", "艾法新(CN)"),
        moa_class="selective-XYZ-receptor-inhibitor",
        source=_src("who-inn-2023", "INN 命名公告", "WHO", "2023-06-01"),
    )
    indication = ev.Indication(
        id="I-XYZ-DIS", version=1, code="XYZ-001",
        label="XYZ 受体过度活化相关疾病",
        source=_src("icd-ref-2023", "适应证口径", "监测办", "2023-09-01",
                    confidentiality=ev.Confidentiality.RESTRICTED),
    )
    candidate = ev.Candidate(
        id="V-ALPHACIN", version=1, compound_id="C-ALPHA-7",
        code="FIC-2025-001", name="阿法西尼制剂",
        originators=("Alpha", "Beta"),
        indications=("I-XYZ-DIS",),
        first_initiated_on="2018-05-01",
        source=_src("alpha-ind-2018", "首次IND申报", "NMPA/FDA", "2018-05-01"),
    )
    discovery = ev.FirstDiscovery(
        id="FD-1", version=1, candidate_id="V-ALPHACIN",
        discoverer="Alpha", discovered_on="2016-11-20",
        novelty="首次报道选择性 XYZ 受体抑制剂的体内疗效",
        evidence_summary="2017 年同行评议论文与原始实验记录",
        source=_src("paper-2017", "Nature-like 期刊论文", "学术期刊", "2017-03-10"),
    )
    for r in (compound, indication, candidate, discovery):
        reg.add(r)

    # ----- 竞争主张：另一机构声称首创（被科学复核驳回） -----------------
    rival_claim = ev.AttributionClaim(
        id="CL-FIC-1", version=1,
        subject_id="Candidate:V-ALPHACIN",
        topic="first_in_class", claimant="Rival Labs",
        assertion="Rival 2016 年内部报告早于 Alpha",
        rationale="未公开、无可核验时间戳的内部笔记",
        status=ev.ClaimStatus.REJECTED,
        resolved_by_review="RV-SCI-ALPHA-2025",
        source=_src("rival-letter-2025", "异议函", "Rival Labs", "2025-08-01"),
    )
    identity_claim = ev.AttributionClaim(
        id="CL-ID-1", version=1,
        subject_id="Compound:C-ALPHA-7",
        topic="identity", claimant="监测岗",
        assertion="EU 商品名 Alfacin 与 CN 艾法新为同一化合物",
        rationale="INN 公告 + 结构式比对一致",
        status=ev.ClaimStatus.PREFERRED,
        source=_src("who-inn-2023", "INN 命名公告", "WHO", "2023-06-01"),
    )
    gap = ev.EvidenceGap(
        id="GAP-1", version=1, subject_id="Candidate:V-ALPHACIN",
        missing="ROW 其他新兴市场 2025 年终端销售尚未取得审计披露",
        blocking=False,
    )
    reg.add(rival_claim)
    reg.add(identity_claim)
    reg.add(gap)

    # ----- 批准（FDA 批准日期之后将被更正，见 add_corrections） ----------
    fda = ev.Approval(
        id="AP-FDA", version=1, candidate_id="V-ALPHACIN",
        indication_id="I-XYZ-DIS", jurisdiction="US/FDA",
        approval_type="NME", approved_on="2025-03-15",
        first_in_class_designation=True,
        source=_src("fda-purple-2025", "FDA 批准信", "FDA", "2025-03-15"),
    )
    ema = ev.Approval(
        id="AP-EMA", version=1, candidate_id="V-ALPHACIN",
        indication_id="I-XYZ-DIS", jurisdiction="EU/EMA",
        approval_type="new-active-substance", approved_on="2025-06-20",
        first_in_class_designation=True,
        source=_src("ema-epar-2025", "EMA EPAR", "EMA", "2025-06-20"),
    )
    reg.add(fda)
    reg.add(ema)

    # ----- 许可/共同开发（按区间与地域） ---------------------------------
    codev = ev.LicenseRelation(
        id="LJ-1", version=1, candidate_id="V-ALPHACIN",
        grantor="Alpha", grantee="Beta", relation_kind="codevelopment",
        regions=("WORLD",),
        interval=Interval("2019-01-01"),
        grantee_share=0.5,
        source=_src("alpha-beta-press-2019", "共同开发公告", "Alpha/Beta", "2019-01-15"),
    )
    license_row = ev.LicenseRelation(
        id="LL-1", version=1, candidate_id="V-ALPHACIN",
        grantor="Alpha", grantee="Beta", relation_kind="license",
        regions=("WW ex-Greater China",),
        interval=Interval("2021-07-01"),
        grantee_share=1.0,
        # 未公开合同：仅持授权人员可见条款
        source=_src("contract-AB-2021", "Alpha-Beta 许可协议（未公开）",
                    "Alpha/Beta", "2021-07-01", locator="§4 地域与分成",
                    confidentiality=ev.Confidentiality.CONFIDENTIAL,
                    contract_grant="grant:contract-AB-2021"),
    )
    reg.add(codev)
    reg.add(license_row)

    # ----- 集团归属：Alpha 2025-04 收购 Gamma -----------------------------
    gamma_standalone = ev.GroupRelation(
        id="GR-GAMMA-1", version=1, unit="Gamma", group="Gamma",
        interval=Interval("2000-01-01", "2025-04-01"),
        source=_src("gamma-reg", "商业登记", "登记机关", "2000-01-01"),
    )
    gamma_alpha = ev.GroupRelation(
        id="GR-GAMMA-2", version=1, unit="Gamma", group="Alpha",
        interval=Interval("2025-04-01"),
        source=_src("ma-alpha-gamma", "收购完成公告", "Alpha", "2025-04-01"),
    )
    reg.add(gamma_standalone)
    reg.add(gamma_alpha)

    # ----- 销售披露 -------------------------------------------------------
    s_beta = ev.SalesDisclosure(
        id="SD-BETA-2025", version=1, candidate_id="V-ALPHACIN",
        reporting_company="Beta", region="WW ex-Greater China",
        amount=700_000_000.0, currency="USD",
        period=Interval("2025-01-01", "2026-01-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.END_MARKET_NET_SALES,
        stream_key="STREAM-ALPHACIN-ROW",
        disclosed_on="2026-02-10",
        source=_src("beta-ar-2025", "Beta 2025 年报", "Beta", "2026-02-10",
                    locator="p.48"),
    )
    s_royalty = ev.SalesDisclosure(
        id="SD-ROYALTY-2025", version=1, candidate_id="V-ALPHACIN",
        reporting_company="Alpha", counterparty="Beta",
        region="WW ex-Greater China",
        amount=84_000_000.0, currency="USD",
        period=Interval("2025-01-01", "2026-01-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.ROYALTY_INCOME,
        stream_key="STREAM-ALPHACIN-ROW",
        disclosed_on="2026-02-20",
        source=_src("alpha-ar-2025", "Alpha 2025 年报", "Alpha", "2026-02-20",
                    locator="特许权费"),
    )
    s_china = ev.SalesDisclosure(
        id="SD-CHINA-2025", version=1, candidate_id="V-ALPHACIN",
        reporting_company="Alpha", region="Greater China",
        amount=3_600_000_000.0, currency="CNY",
        period=Interval("2025-01-01", "2026-01-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.END_MARKET_NET_SALES,
        stream_key="STREAM-ALPHACIN-CN",
        disclosed_on="2026-02-20",
        source=_src("alpha-ar-2025", "Alpha 2025 年报", "Alpha", "2026-02-20",
                    locator="中国区销售"),
    )
    s_japan = ev.SalesDisclosure(
        id="SD-JP-FY2025", version=1, candidate_id="V-ALPHACIN",
        reporting_company="NihonPharma", region="Japan",
        amount=36_000_000_000.0, currency="JPY",
        # 3 月结日财年 FY2025：2024-04-01 ~ 2025-04-01，按天分摊
        period=Interval("2024-04-01", "2025-04-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.END_MARKET_NET_SALES,
        stream_key="STREAM-ALPHACIN-JP",
        disclosed_on="2026-05-15",
        source=_src("nihon-ar-fy2025", "日本合作方 FY2025 年报",
                    "NihonPharma", "2026-05-15"),
    )
    s_intercompany = ev.SalesDisclosure(
        id="SD-INTERCO-2025", version=1, candidate_id="V-ALPHACIN",
        reporting_company="Alpha", counterparty="Gamma",
        region="Greater China",
        amount=100_000_000.0, currency="USD",
        period=Interval("2025-05-01", "2025-08-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.INTERCOMPANY_SALES,
        stream_key="STREAM-ALPHACIN-INTERCO",
        disclosed_on="2026-02-20",
        source=_src("alpha-ar-2025", "Alpha 2025 年报（关联交易附注）",
                    "Alpha", "2026-02-20"),
    )
    for r in (s_beta, s_royalty, s_china, s_japan, s_intercompany):
        reg.add(r)

    # ----- 双重复核（科学 + 统计） ---------------------------------------
    rv_sci = Review(
        review_id="RV-SCI-ALPHA-2025", kind="scientific",
        period_code="2025", candidate_id="V-ALPHACIN",
        reviewer="李科学", reviewer_org="科学咨询委员会",
        decision="approved",
        findings="XYZ 机制首创成立；Rival 主张无可核验时间戳，驳回；别名同一化合物",
        reviewed_at="2026-03-05",
        inputs=("FirstDiscovery:FD-1", "Approval:AP-FDA", "AttributionClaim:CL-FIC-1"),
    )
    rv_stat = Review(
        review_id="RV-STAT-ALPHA-2025", kind="statistical",
        period_code="2025", candidate_id="V-ALPHACIN",
        reviewer="王统计", reviewer_org="统计与核算组",
        decision="approved",
        findings="销售流去重与内部交易抵销正确；财年/币种按 rules-v1 归一",
        reviewed_at="2026-03-10",
        inputs=("SalesDisclosure:SD-BETA-2025", "SalesDisclosure:SD-CHINA-2025",
                "RuleBook:rules-v1"),
    )
    reg.add_review(rv_sci)
    reg.add_review(rv_stat)

    engine = Engine(reg)
    handles = {
        "period": PERIOD,
        "candidate": "V-ALPHACIN",
        "compound": "C-ALPHA-7",
        "disclosures": [s.id for s in (s_beta, s_royalty, s_china, s_japan, s_intercompany)],
        "grants": {"contract": "grant:contract-AB-2021"},
    }
    return reg, engine, handles


def add_post_freeze_events(reg: Registry) -> dict:
    """冻结之后到达的事件：重述、批准日期更正、后到海外证据。"""
    # 1) Beta 重述 2025 年报：ROW 终端销售 700m -> 720m
    restated = ev.SalesDisclosure(
        id="SD-BETA-2025", version=2, supersedes="SD-BETA-2025#v1",
        recorded_at="2026-09-01",
        candidate_id="V-ALPHACIN",
        reporting_company="Beta", region="WW ex-Greater China",
        amount=720_000_000.0, currency="USD",
        period=Interval("2025-01-01", "2026-01-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.END_MARKET_NET_SALES,
        stream_key="STREAM-ALPHACIN-ROW",
        disclosed_on="2026-09-01",
        source=_src("beta-ar-2025-r", "Beta 2025 年报（重述版）",
                    "Beta", "2026-09-01", locator="p.48 rev"),
        note="审计调整：年末截单差异 +20m",
    )
    reg.add(restated)

    # 2) FDA 更正批准日期：3-15 -> 3-12
    fda_v2 = ev.Approval(
        id="AP-FDA", version=2, supersedes="AP-FDA#v1",
        recorded_at="2026-09-05",
        candidate_id="V-ALPHACIN", indication_id="I-XYZ-DIS",
        jurisdiction="US/FDA", approval_type="NME",
        approved_on="2025-03-12", first_in_class_designation=True,
        source=_src("fda-corr-2026", "FDA 批准日期更正函", "FDA", "2026-09-05"),
    )
    reg.add(fda_v2)

    # 3) 后到的欧洲终端销售证据（披露日在 rules-v2 生效之后，适用 v2）
    late_eu = ev.SalesDisclosure(
        id="SD-EU-2025-LATE", version=1, recorded_at="2026-09-10",
        candidate_id="V-ALPHACIN",
        reporting_company="Beta", region="Europe",
        amount=46_000_000.0, currency="EUR",
        period=Interval("2025-01-01", "2026-01-01"),
        fiscal_year_label="2025",
        revenue_kind=ev.RevenueKind.END_MARKET_NET_SALES,
        stream_key="STREAM-ALPHACIN-EU",
        disclosed_on="2026-09-10",
        source=_src("beta-supp-eu-2025", "Beta 补充披露：欧洲经销数据",
                    "Beta", "2026-09-10"),
    )
    reg.add(late_eu)
    return {"restated": "SD-BETA-2025#v2", "fda_v2": "AP-FDA#v2",
            "late_eu": "SD-EU-2025-LATE#v1"}


def viewers() -> dict[str, Viewer]:
    return {
        "public": Viewer(user="public-monitor"),
        "analyst": Viewer(
            user="zhao",
            grants=frozenset({"grant:contract-AB-2021"}),
            clearance=ev.Confidentiality.CONFIDENTIAL,
        ),
    }
