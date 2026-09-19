"""计算引擎：FIC 判定、双重复核、销售去重/抵销/归一与结论装配。

引擎是**无状态**的：所有结论都由某个 :class:`~evidence_db.registry.EvidenceView`
（即某个事件序号下的证据）加一版规则推导，自身不落任何数字。因此：

* 用当前视图算 → 得到待发布结论；
* 用冻结快照记录的事件序号重建视图再算 → 逐位复现已发布年度数字；
* 监测人员无法手填汇总数——没有披露来源腿的金额根本进不了结论。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import evidence as ev
from .registry import EvidenceView, Registry, SourceChainViolation
from .rules import RuleBookVersion
from .timeline import Interval, StatsPeriod, annual_period, d


# ---------------------------------------------------------------------------
# 复核
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Review:
    """一次复核。科学复核与统计复核都必须通过才能冻结。"""

    review_id: str
    kind: str                       # scientific | statistical
    period_code: str
    candidate_id: str
    reviewer: str
    reviewer_org: str
    decision: str                   # approved | rejected | changes_requested
    findings: str
    reviewed_at: str
    inputs: tuple[str, ...] = ()    # 复核所依据的记录键/规则版本

    def digest(self) -> str:
        payload = {k: v for k, v in dataclasses.asdict(self).items()}
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


# ---------------------------------------------------------------------------
# 访问控制
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Viewer:
    """监测人员的访问上下文。未公开合同（confidential）只对授权人员开放。"""

    user: str
    grants: frozenset[str] = frozenset()
    clearance: ev.Confidentiality = ev.Confidentiality.PUBLIC

    def can_see(self, source: Optional[ev.Source]) -> bool:
        if source is None:
            return True
        levels = {
            ev.Confidentiality.PUBLIC: 0,
            ev.Confidentiality.RESTRICTED: 1,
            ev.Confidentiality.CONFIDENTIAL: 2,
        }
        if levels[source.confidentiality] > levels[self.clearance]:
            return False
        if source.confidentiality == ev.Confidentiality.CONFIDENTIAL:
            return source.contract_grant in self.grants
        return True


# ---------------------------------------------------------------------------
# 销售腿
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SalesLeg:
    stream_key: str
    region: str
    amount_base: float
    currency: str
    revenue_kind: str
    disclosure_ids: tuple[str, ...]
    rule_version: str
    included: bool
    reason: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class Engine:
    BASE_FOR_BLOCKBUSTER = "USD"

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    # ===== 品种结论 =======================================================

    def conclude(self, period: StatsPeriod, candidate_id: str,
                 view: Optional[EvidenceView] = None,
                 frozen_override: Optional[bool] = None) -> dict:
        view = view or self.registry.view_as_of()
        cand = view.by_id(ev.Candidate, candidate_id)
        if cand is None:
            raise KeyError(f"候选药不存在: {candidate_id}")

        compound = view.by_id(ev.Compound, cand.compound_id)
        aliases = compound.aliases if compound else ()

        fic = self._assess_fic(period, cand, compound, view)
        sales = self._assemble_sales(period, cand, view)
        licensing = self._assemble_licensing(period, cand, view)
        gaps = self._collect_gaps(cand, view)

        # 复核状态（科学 + 统计 双重复核）
        reviews = self._reviews_for(period.code, candidate_id, view)
        # 复现历史冻结时按当时状态，不读取“现在是否已冻结”
        frozen = frozen_override if frozen_override is not None \
            else self.registry.is_frozen(period.code, candidate_id)
        dual_approved = (
            reviews.get("scientific", False)
            and reviews.get("statistical", False)
        )

        blockbuster = sales["total_base"] >= 1_000_000_000.0 and sales["currency_base"] == "USD"
        # 计入首创指标：FIC 且（双重复核通过，或系复现已冻结的历史结论）
        included = bool(fic["is_first_in_class"] and (dual_approved or frozen)
                        and not fic["blocking_conflict"]
                        and not any(g["blocking"] for g in gaps))

        deps = self._dependency_keys(cand, view)
        deps.update(f"Review:{rid}" for rid in reviews["ids"])
        self.registry.remember_lineage(period.code, candidate_id, deps)

        return {
            "candidate_id": cand.id,
            "candidate_name": cand.name,
            "compound_id": cand.compound_id,
            "aliases": list(aliases),
            "period_code": period.code,
            "included": included,
            "first_in_class": bool(fic["is_first_in_class"]),
            "fic_basis": fic["basis"],
            "competing_claims": fic["claims"],
            "licensing": licensing,
            "global_sales": {
                "base_currency": sales["currency_base"],
                "deduped_total": round(sales["total_base"], 2),
                "over_blockbuster": blockbuster,
            },
            "sales_legs": [leg.as_dict() for leg in sales["legs"]],
            "rule_versions": sales["rule_versions"],
            "reviews": {
                "scientific": reviews.get("scientific", False),
                "statistical": reviews.get("statistical", False),
                "review_ids": reviews["ids"],
            },
            "gaps": gaps,
            "frozen": frozen,
            "input_digests": sales["input_digests"],
        }

    # ----- FIC 判定 -------------------------------------------------------

    def _assess_fic(self, period, cand, compound, view) -> dict:
        discoveries = [r for r in view.all_latest(ev.FirstDiscovery)
                       if r.candidate_id == cand.id]
        approvals = [r for r in view.all_latest(ev.Approval)
                     if r.candidate_id == cand.id]
        claims = [r for r in view.all_latest(ev.AttributionClaim)
                  if r.subject_id in (f"Candidate:{cand.id}", f"Compound:{cand.compound_id}")
                  and r.topic in ("first_in_class", "identity")]

        # 所有竞争主张都保留在结论中（含被驳回者），裁决状态可追溯；
        # 只有尚未裁决的首创异议会阻塞计入。
        blocking_conflict = any(
            c.status == ev.ClaimStatus.OPEN and c.topic == "first_in_class"
            for c in claims
        )

        moa = compound.moa_class if compound else ""
        # 同机制类别中谁最早
        class_peers = self._same_class_peers(moa, view)
        earliest_approval_here = min(
            (a.approved_on for a in approvals), default=None
        )
        peer_earliest = None
        for peer_id in class_peers - {cand.id}:
            peer_appr = [a.approved_on for a in view.all_latest(ev.Approval)
                         if a.candidate_id == peer_id]
            if peer_appr:
                peer_earliest = min(peer_appr + ([peer_earliest] if peer_earliest else []))

        designation = any(a.first_in_class_designation for a in approvals)
        earliest_discovery = min((x.discovered_on for x in discoveries), default=None)

        is_fic = False
        basis_parts = []
        if designation:
            is_fic = True
            basis_parts.append("监管机构明示同类首创（first-in-class designation）")
        if earliest_discovery:
            is_fic = True
            basis_parts.append(f"最早关键发现 {earliest_discovery}：{discoveries[0].novelty}")
        if earliest_approval_here and (
            peer_earliest is None or earliest_approval_here <= peer_earliest
        ) and moa:
            is_fic = True
            basis_parts.append(
                f"机制类别「{moa}」内全球最早批准 {earliest_approval_here}"
            )
        if peer_earliest and earliest_approval_here and earliest_approval_here > peer_earliest:
            is_fic = False
            basis_parts.append(f"同类已有更早批准 {peer_earliest}，不支持首创")

        return {
            "is_first_in_class": is_fic and not blocking_conflict,
            "basis": "；".join(basis_parts) or "证据不足",
            "claims": [
                {
                    "claim_id": c.id,
                    "claimant": c.claimant,
                    "assertion": c.assertion,
                    "status": c.status.value,
                    "resolved_by_review": c.resolved_by_review,
                }
                for c in claims
            ],
            "blocking_conflict": blocking_conflict,
        }

    def _same_class_peers(self, moa: str, view: EvidenceView) -> set[str]:
        if not moa:
            return set()
        peers = set()
        for c in view.all_latest(ev.Candidate):
            comp = view.by_id(ev.Compound, c.compound_id)
            if comp and comp.moa_class == moa:
                peers.add(c.id)
        return peers

    # ----- 销售归集 -------------------------------------------------------

    def _assemble_sales(self, period, cand, view) -> dict:
        disclosures = [r for r in view.all_latest(ev.SalesDisclosure)
                       if r.candidate_id == cand.id]
        legs: list[SalesLeg] = []
        input_digests: dict[str, str] = {}
        rule_versions: dict[str, str] = {}

        # 1) 按销售流分组，先做内部交易抵销与期间/币种归一
        per_stream: dict[str, list[dict]] = {}
        for disc in disclosures:
            book = view.rules.effective_on(disc.disclosed_on)
            rule_versions[disc.disclosed_on] = book.version
            input_digests[f"SalesDisclosure:{disc.id}"] = disc.digest()

            span = self._effective_span(disc, book)
            geo = book.geo.canonical(disc.region)

            reason = ""
            included = True
            # 内部销售：性质标注或双方同属一个集团（按披露期间判定）
            if disc.revenue_kind == ev.RevenueKind.INTERCOMPANY_SALES:
                included, reason = False, "集团内部销售，按性质抵销"
            elif disc.counterparty and self._same_group_during(
                    disc.reporting_company, disc.counterparty, span, view):
                included, reason = False, "披露期间双方同属一个集团，内部交易抵销"

            # 里程碑、合作利润分成不构成年度产品销售额
            if disc.revenue_kind == ev.RevenueKind.MILESTONE:
                included, reason = False, "里程碑款不计入年度产品销售额"

            amount_base = self._convert_into_period(disc.amount, span, disc.currency,
                                                    book, period)
            if amount_base is None:
                included, reason = False, "金额不在统计期内"
                amount_base = 0.0

            per_stream.setdefault(disc.stream_key, []).append({
                "disc": disc, "geo": geo, "amount": amount_base,
                "included_pre": included, "reason_pre": reason,
                "rule": book.version,
            })

        # 2) 流内去重：终端销售腿优先，特许权费代表同一终端销售，不重复加总
        currency_base = "USD"
        if view.rules.versions():
            currency_base = view.rules.versions()[-1].fx.base_currency

        total = 0.0
        for stream, items in per_stream.items():
            end_legs = [
                it for it in items
                if it["included_pre"]
                and it["disc"].revenue_kind in (
                    ev.RevenueKind.END_MARKET_NET_SALES,
                    ev.RevenueKind.END_MARKET_GROSS_SALES,
                )
            ]
            # 同一规范地域内的多条终端腿：保留披露日最新的一条，其余记为重复
            seen_region: dict[str, dict] = {}
            for it in end_legs:
                prev = seen_region.get(it["geo"])
                if prev is None or it["disc"].disclosed_on > prev["disc"].disclosed_on:
                    seen_region[it["geo"]] = it
            keep_ids: set[str] = set()
            for it in seen_region.values():
                keep_ids.add(it["disc"].id)

            for it in items:
                disc = it["disc"]
                kind = disc.revenue_kind
                include = it["included_pre"]
                reason = it["reason_pre"]
                if include and kind in (ev.RevenueKind.END_MARKET_NET_SALES,
                                        ev.RevenueKind.END_MARKET_GROSS_SALES):
                    if disc.id not in keep_ids:
                        include, reason = False, f"{it['geo']} 地域重复腿，保留最新披露"
                    else:
                        total += it["amount"]
                elif include and kind == ev.RevenueKind.ROYALTY_INCOME:
                    if end_legs:
                        include, reason = False, "该销售流已有终端销售腿，特许权费不重复计入"
                    else:
                        # 仅有特许权费时作为该流终端规模的保守口径并标注
                        reason = "仅有特许权费口径，按其折算（待终端销售证据）"
                        total += it["amount"]
                elif include and kind == ev.RevenueKind.COLLABORATION_PROFIT_SHARE:
                    include, reason = False, "合作利润分成不单独加总（避免与终端销售重复）"

                legs.append(SalesLeg(
                    stream_key=stream,
                    region=it["geo"],
                    amount_base=round(it["amount"], 2),
                    currency=disc.currency,
                    revenue_kind=kind.value,
                    disclosure_ids=(disc.id,),
                    rule_version=it["rule"],
                    included=include,
                    reason=reason or ("计入" if include else "不计入"),
                ))

        legs.sort(key=lambda x: (x.stream_key, x.region, x.disclosure_ids[0]))
        return {
            "legs": legs,
            "total_base": total,
            "currency_base": currency_base,
            "rule_versions": rule_versions,
            "input_digests": input_digests,
        }

    @staticmethod
    def _effective_span(disc: ev.SalesDisclosure, book: RuleBookVersion) -> Interval:
        if disc.fiscal_year_label:
            span = book.fiscal_interval(disc.reporting_company, disc.fiscal_year_label)
            if span is not None:
                return span
        return disc.period

    @staticmethod
    def _convert_into_period(amount, span, currency, book, period) -> Optional[float]:
        """按统计期切出金额，再按各日历年的年均汇率折算基准币。"""
        overlap = span.intersect(period.interval)
        if overlap is None:
            return None
        total_days = span.day_count()
        base = 0.0
        cur = d(overlap.start)
        end = d(overlap.end)
        while cur < end:
            year_end = date(cur.year + 1, 1, 1)
            stop = min(end, year_end)
            days = (stop - cur).days
            seg_amount = amount * days / total_days
            base += book.fx.to_base(seg_amount, currency, cur.year)
            cur = stop
        return base

    @staticmethod
    def _same_group_during(company_a: str, company_b: str, span: Interval,
                           view: EvidenceView) -> bool:
        rels = [r for r in view.all_latest(ev.GroupRelation)
                if r.unit in (company_a, company_b)]
        # 在披露区间内取若干采样日，判断是否曾同属一个集团。
        # 母公司自身可能没有归属记录，集团名默认等于公司名本身。
        groups: dict[str, set[str]] = {
            company_a: {company_a},
            company_b: {company_b},
        }
        for r in rels:
            ov = r.interval.intersect(span)
            if ov is not None:
                groups.setdefault(r.unit, {r.unit}).add(r.group)
        return bool(groups.get(company_a, set()) & groups.get(company_b, set()))

    # ----- 缺口与复核 -----------------------------------------------------

    @staticmethod
    def _assemble_licensing(period, cand, view) -> list[dict]:
        """共同开发/跨区许可关系（按生效区间）。未公开合同标注保密等级。"""
        out = []
        for r in view.all_latest(ev.LicenseRelation):
            if r.candidate_id != cand.id:
                continue
            active = r.interval.intersect(period.interval) is not None
            src = r.source
            out.append({
                "relation_id": r.id,
                "kind": r.relation_kind,
                "grantor": r.grantor,
                "grantee": r.grantee,
                "regions": list(r.regions),
                "interval": dataclasses.asdict(r.interval),
                "grantee_share": r.grantee_share,
                "active_in_period": active,
                "source": {
                    "doc_id": src.doc_id,
                    "title": src.title,
                    "confidentiality": src.confidentiality.value,
                    "contract_grant": src.contract_grant,
                } if src is not None else None,
            })
        out.sort(key=lambda x: (x["interval"]["start"], x["relation_id"]))
        return out

    @staticmethod
    def _collect_gaps(cand, view) -> list[dict]:
        out = []
        for g in view.all_latest(ev.EvidenceGap):
            if g.subject_id in (f"Candidate:{cand.id}", f"Compound:{cand.compound_id}"):
                out.append({"gap_id": g.id, "missing": g.missing, "blocking": g.blocking})
        return out

    def _reviews_for(self, period_code, candidate_id, view) -> dict:
        sci = stat = False
        ids: list[str] = []
        for rid in view.review_digests:
            review = self.registry.review(rid)
            if review is None:
                continue
            if review.period_code == period_code and review.candidate_id == candidate_id:
                ids.append(rid)
                if review.kind == "scientific" and review.approved:
                    sci = True
                if review.kind == "statistical" and review.approved:
                    stat = True
        return {"scientific": sci, "statistical": stat, "ids": sorted(ids)}

    @staticmethod
    def _dependency_keys(cand, view) -> set[str]:
        deps = {f"Candidate:{cand.id}", f"Compound:{cand.compound_id}"}
        cid = cand.id
        for r in view.all_latest(ev.Approval):
            if r.candidate_id == cid:
                deps.add(f"Approval:{r.id}")
                if r.indication_id:
                    deps.add(f"Indication:{r.indication_id}")
        for t in (ev.FirstDiscovery, ev.SalesDisclosure, ev.LicenseRelation):
            for r in view.all_latest(t):
                if r.candidate_id == cid:
                    deps.add(f"{t.__name__}:{r.id}")
        for t in (ev.AttributionClaim, ev.EvidenceGap):
            for r in view.all_latest(t):
                if r.subject_id in (f"Candidate:{cid}", f"Compound:{cand.compound_id}"):
                    deps.add(f"{t.__name__}:{r.id}")
        return deps

    # ===== 指标汇总 =======================================================

    def first_approvals_in_period(self, period: StatsPeriod,
                                  view: Optional[EvidenceView] = None) -> set[str]:
        """分母：统计期内取得**全球首次任一法域批准**的品种。"""
        view = view or self.registry.view_as_of()
        result = set()
        for a in view.all_latest(ev.Approval):
            if period.contains(a.approved_on):
                # 该品种此前没有任何批准
                earlier = [x for x in view.all_latest(ev.Approval)
                           if x.candidate_id == a.candidate_id and x.approved_on < a.approved_on]
                if not earlier:
                    result.add(a.candidate_id)
        return result

    def indicators(self, period: StatsPeriod,
                   view: Optional[EvidenceView] = None) -> dict:
        """只从结论派生指标，绝不接受外部传入的汇总数。"""
        view = view or self.registry.view_as_of()
        candidates = {c.id for c in view.all_latest(ev.Candidate)}
        conclusions = [self.conclude(period, cid, view) for cid in sorted(candidates)]

        first_approved = self.first_approvals_in_period(period, view)
        fic_count = sum(1 for c in conclusions
                        if c["included"] and c["first_in_class"]
                        and c["candidate_id"] in first_approved)
        denominator = len(first_approved)
        share = (fic_count / denominator * 100.0) if denominator else 0.0

        blockbuster_varieties = sorted(
            c["candidate_id"] for c in conclusions
            if c["global_sales"]["over_blockbuster"]
        )
        return {
            "period_code": period.code,
            "fic-global-share": {
                "value": round(share, 2),
                "unit": "percent",
                "numerator_fic": fic_count,
                "denominator_first_approvals": denominator,
            },
            "blockbuster-varieties": {
                "value": len(blockbuster_varieties),
                "unit": "count",
                "threshold": "USD 1,000,000,000 annual global end-market sales",
                "candidate_ids": blockbuster_varieties,
            },
        }

    # ===== 冻结 ===========================================================

    def freeze_period(self, period: StatsPeriod, frozen_at: str) -> dict:
        """复核齐备后冻结整个统计期。"""
        view = self.registry.view_as_of()
        candidates = {c.id for c in view.all_latest(ev.Candidate)}
        # 快照语义即“已冻结”，统一置位，保证日后按事件序号复现时指纹一致。
        conclusions = {
            cid: self.conclude(period, cid, view, frozen_override=True)
            for cid in sorted(candidates)
        }

        # 所有将计入的品种必须双重复核通过
        review_ids: list[str] = []
        for cid, payload in conclusions.items():
            revs = payload["reviews"]
            if payload["included"] and payload["first_in_class"]:
                if not (revs["scientific"] and revs["statistical"]):
                    raise SourceChainViolation(
                        f"品种 {cid} 拟计入首创指标但科学/统计复核未齐备，禁止冻结"
                    )
            review_ids.extend(revs["review_ids"])

        indicator_map = self._indicator_scalars(period, view)
        rule_versions: dict[str, str] = {}
        for payload in conclusions.values():
            rule_versions.update(payload["rule_versions"])

        snapshot = self.registry.freeze(
            period=period,
            frozen_at=frozen_at,
            conclusions=conclusions,
            indicators=indicator_map,
            rule_versions=rule_versions,
            review_ids=sorted(set(review_ids)),
        )
        return snapshot.to_dict()

    def _indicator_scalars(self, period, view) -> dict[str, float]:
        ind = self.indicators(period, view)
        return {
            "fic-global-share": ind["fic-global-share"]["value"],
            "blockbuster-varieties": float(ind["blockbuster-varieties"]["value"]),
        }

    def verify_published(self, period: StatsPeriod, freeze_id: str) -> bool:
        snapshot = self.registry.get_freeze(period.code, freeze_id)

        def recompute(period_code: str, view) -> dict:
            p = annual_period(int(period_code))
            return {
                cid: self.conclude(p, cid, view, frozen_override=True)
                for cid in {c.id for c in view.all_latest(ev.Candidate)}
            }

        return self.registry.verify_freeze(snapshot, recompute)

    # ===== 面向访问者的脱敏投影 ==========================================

    def visible_conclusion(self, period: StatsPeriod, candidate_id: str,
                           viewer: Viewer) -> dict:
        """按访问权限投影结论：未公开合同细节对未授权人员脱敏。

        脱敏只隐藏合同条款出处（doc/章节/授权标识），经济结论（区间、地域、
        分成、去重后全球销售）仍可展示；金额汇总本来就不依赖条款文本。
        """
        payload = json.loads(json.dumps(self.conclude(period, candidate_id),
                                        ensure_ascii=False))
        view = self.registry.view_as_of()
        redacted: list[str] = []

        # 保密的许可/共同开发合同
        for rel in payload.get("licensing", []):
            src = rel.get("source")
            if src and src["confidentiality"] == ev.Confidentiality.CONFIDENTIAL.value:
                full = view.by_id(ev.LicenseRelation, rel["relation_id"])
                if full and full.source and not viewer.can_see(full.source):
                    redacted.append(f"LicenseRelation:{rel['relation_id']}")
                    rel["source"] = {
                        "doc_id": "redacted",
                        "title": "未公开合同（未授权）",
                        "confidentiality": "confidential",
                        "contract_grant": None,
                    }

        # 保密来源支撑的销售披露
        redacted_disc = []
        for leg in payload["sales_legs"]:
            new_ids = []
            for did in leg["disclosure_ids"]:
                disc = view.by_id(ev.SalesDisclosure, did)
                if disc and disc.source and not viewer.can_see(disc.source):
                    redacted_disc.append(did)
                    new_ids.append(f"redacted:{did}")
                else:
                    new_ids.append(did)
            leg["disclosure_ids"] = new_ids
        redacted.extend(f"SalesDisclosure:{x}" for x in redacted_disc)

        if redacted:
            payload["access"] = {
                "viewer": viewer.user,
                "redacted": sorted(set(redacted)),
                "notice": "存在未公开合同支撑的证据，其来源细节不展示，经济结论不变",
            }
        return payload
