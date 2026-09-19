"""销售合并：权利链去重、集团内部抵销、币种/财年/地域归一、冲突挂缺口。

设计原则（对应规划监测要求）：

* 只认证据，不认手填汇总：每条纳入/剔除都回链到具体披露版本与规则版本。
* 谁的权利区间有效谁计"产品销售"：按带生效区间的许可/转手/并购关系确定
  权利持有人；让渡方在让渡区间只保留权利金，权利金不重复计入全球销售。
* 集团内部交易抵销：同一时点同一集团内的销售整条剔除。
* 币种按规则版本折算到锚币种；财年标签归一到统计期间；地域按口径表归一。
* 同一(地域,期间)出现无法用权利链解释的多方产品销售 → 竞争性冲突，
  整条挂"待确认证据"，在有采纳意见前不计入（宁可留缺口，不臆造拆分）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .model import (
    REL_ACQUISITION,
    REL_ASSIGNMENT,
    REL_CODEVELOPMENT,
    REL_LICENSE,
    REV_INTERCOMPANY,
    REV_PARTNER_SHARE,
    REV_ROYALTY,
    Candidate,
    Relation,
    SalesDisclosure,
)
from .rules import RuleBookVersion
from .store import Actor, EvidenceStore

# 发布计算以系统主体进行（可见未公开合同，但只产出脱敏后的发布结论）
SYSTEM = Actor.system()

# 单条披露的处置状态
ST_INCLUDED = "included"
ST_EXCLUDED = "excluded"
ST_PARTIAL = "partial"

# 剔除/调整原因
R_RESTATED = "restated-superseded"
R_INTERCOMPANY = "intra-group-eliminated"
R_ROYALTY = "royalty-after-rights-transfer"
R_CONFLICT = "unresolved-attribution-conflict"
R_RULE_GAP = "conversion-rule-gap"
R_PARTNER_SHARE = "partner-share-removed"
R_RESTRICTED = "restricted-evidence-invisible"


@dataclass
class SalesLine:
    """单条销售披露在合并中的处置结果（含完整溯源）。"""

    disclosure_id: str
    version: int
    party_id: str
    raw_amount: float
    raw_currency: str
    raw_region: str
    raw_fiscal_label: str
    period_key: Optional[str] = None
    region: Optional[str] = None
    anchor_amount_gross: float = 0.0
    anchor_amount_included: float = 0.0
    status: str = ST_EXCLUDED
    reasons: List[str] = field(default_factory=list)
    adjustments: List[Dict[str, Any]] = field(default_factory=list)
    fx: Optional[Dict[str, Any]] = None
    source_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "version": self.version,
            "party_id": self.party_id,
            "raw": {
                "amount": self.raw_amount,
                "currency": self.raw_currency,
                "region": self.raw_region,
                "fiscal_label": self.raw_fiscal_label,
            },
            "normalized": {"period_key": self.period_key, "region": self.region},
            "anchor_amount_gross": self.anchor_amount_gross,
            "anchor_amount_included": self.anchor_amount_included,
            "status": self.status,
            "reasons": self.reasons,
            "adjustments": self.adjustments,
            "fx": self.fx,
            "source_id": self.source_id,
        }


@dataclass
class ConsolidationResult:
    candidate_id: str
    anchor_ccy: str
    rulebook_version: str
    global_sales_anchor: float = 0.0
    by_period: Dict[str, float] = field(default_factory=dict)
    by_region: Dict[str, float] = field(default_factory=dict)
    lines: List[SalesLine] = field(default_factory=list)
    eliminations: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[Dict[str, Any]] = field(default_factory=list)

    def included_lines(self) -> List[SalesLine]:
        return [l for l in self.lines if l.status in (ST_INCLUDED, ST_PARTIAL)]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "anchor_ccy": self.anchor_ccy,
            "rulebook_version": self.rulebook_version,
            "global_sales_anchor": self.global_sales_anchor,
            "by_period": self.by_period,
            "by_region": self.by_region,
            "lines": [l.to_dict() for l in self.lines],
            "eliminations": self.eliminations,
            "conflicts": self.conflicts,
            "gaps": self.gaps,
        }


class _RightsResolver:
    """根据带生效区间的许可/转手/并购关系，判断任一时点、地域的权利状态。"""

    def __init__(self, relations: List[Relation]):
        self._relations = relations

    def _covering(self, party_a: str, party_b: str, region: str, on_day: str):
        out = []
        for rel in self._relations:
            if not rel.valid.contains(on_day):
                continue
            if not _region_covers(rel.regions, region):
                continue
            pair = {rel.grantor_party_id, rel.grantee_party_id}
            if party_a in pair and party_b in pair:
                out.append(rel)
        return out

    def right_transfer(self, seller: str, other: str, region: str, on_day: str):
        """other 与 seller 之间在该时点/地域是否存在许可或转手关系。"""
        for rel in self._covering(seller, other, region, on_day):
            if rel.relation_type in (REL_LICENSE, REL_ASSIGNMENT):
                return rel
        return None

    def codeveloped(self, seller: str, other: str, region: str, on_day: str) -> bool:
        return any(
            rel.relation_type == REL_CODEVELOPMENT
            for rel in self._covering(seller, other, region, on_day)
        )

    def is_royalty_counterparty(self, seller: str, other: str, region: str, on_day: str) -> bool:
        """other 已取得该区域产品销售权，seller 只应确认权利金。"""
        rel = self.right_transfer(seller, other, region, on_day)
        if rel is None:
            return False
        # seller 是出让/许可方，权利已让渡给 other
        return rel.grantor_party_id == seller and rel.grantee_party_id == other


def _region_covers(licensed_regions: List[str], region: str) -> bool:
    if not licensed_regions:
        return True  # 空地域 = 全球
    target = region.upper()
    for r in licensed_regions:
        if r.upper() in ("WORLD", "GLOBAL", "ALL"):
            return True
        if r.upper() == target:
            return True
    return False


def _build_group_membership(
    relations: List[Relation], parties_by_id: Dict[str, Any]
) -> Dict[Tuple[str, str], Optional[str]]:
    """返回 (party_id, on_day) -> 该时点所属集团。

    静态归属取 Party.group_id；并购关系自生效日起把标的并入收购方集团。
    """
    # 收集改变归属的事件：(date, target_party, new_group)
    events: Dict[str, List[Tuple[str, str]]] = {}
    for rel in relations:
        if rel.relation_type != REL_ACQUISITION:
            continue
        start = rel.valid.start
        if start is None:
            continue
        target = rel.grantee_party_id  # 被收购方
        # 收购方集团
        acquirer = parties_by_id.get(rel.grantor_party_id)
        new_group = acquirer.group_id if acquirer is not None else rel.grantor_party_id
        events.setdefault(target or "", []).append((start, new_group or rel.grantor_party_id))
    for seq in events.values():
        seq.sort()

    def group_of(party_id: str, on_day: str) -> Optional[str]:
        party = parties_by_id.get(party_id)
        base = party.group_id if party is not None else party_id
        current = base
        for eff_date, new_group in events.get(party_id, ()):  # 已升序
            if eff_date <= on_day:
                current = new_group
            else:
                break
        return current

    return _MemoGroup(group_of)  # type: ignore


class _MemoGroup:
    def __init__(self, fn):
        self._fn = fn
        self._cache: Dict[Tuple[str, str], Optional[str]] = {}

    def __call__(self, party_id: str, on_day: str) -> Optional[str]:
        key = (party_id, on_day)
        if key not in self._cache:
            self._cache[key] = self._fn(party_id, on_day)
        return self._cache[key]


def consolidate_sales(
    store: EvidenceStore,
    candidate: Candidate,
    rulebook: RuleBookVersion,
    *,
    as_of: Optional[str] = None,
    actor: Optional[Actor] = SYSTEM,
) -> ConsolidationResult:
    """合并某候选药在 as_of 时点可见的全部销售披露。"""
    cid = candidate.id
    result = ConsolidationResult(
        candidate_id=cid,
        anchor_ccy=rulebook.anchor_ccy,
        rulebook_version=rulebook.version,
    )

    disclosures: List[SalesDisclosure] = [
        d for d in store.iter_kind("salesdisclosure", as_of=as_of, actor=actor)
        if getattr(d, "redacted", False) is False and d.candidate_id == cid
    ]
    relations: List[Relation] = [
        r for r in store.iter_kind("relation", as_of=as_of, actor=actor)
        if getattr(r, "redacted", False) is False and r.candidate_id == cid
    ]
    parties = {p.id: p for p in store.iter_kind("party", as_of=as_of, actor=actor)
               if getattr(p, "redacted", False) is False}
    rights = _RightsResolver(relations)
    group_of = _build_group_membership(relations, parties)

    # 被在场新披露重述的旧披露 id（重述链）
    superseded = {d.restatement_of for d in disclosures if d.restatement_of}

    # ---- 逐条归一 ----
    lines: Dict[str, SalesLine] = {}
    for d in disclosures:
        rec = store.version_at(d.id, as_of)
        version_no = rec.version if rec else 0
        line = SalesLine(
            disclosure_id=d.id,
            version=version_no,
            party_id=d.party_id or "",
            raw_amount=d.amount,
            raw_currency=d.currency,
            raw_region=d.region,
            raw_fiscal_label=d.fiscal_year_label,
            source_id=d.source_id,
        )

        # 1) 被重述 → 整条剔除（保留可复现）
        if d.id in superseded:
            line.reasons.append(R_RESTATED)
            lines[d.id] = line
            continue

        on_day = d.period_end or d.period_start or ""

        # 2) 集团内部交易抵销
        if d.revenue_kind == REV_INTERCOMPANY or (
            d.counterparty_party_id and on_day and group_of(d.party_id, on_day)
            and group_of(d.party_id, on_day) == group_of(d.counterparty_party_id, on_day)
        ):
            line.reasons.append(R_INTERCOMPANY)
            result.eliminations.append({
                "disclosure_id": d.id,
                "party_id": d.party_id,
                "counterparty_party_id": d.counterparty_party_id,
                "group_id": group_of(d.party_id, on_day) if on_day else None,
                "raw_amount": d.amount,
                "currency": d.currency,
            })
            lines[d.id] = line
            continue

        # 3) 财年 / 地域 / 币种归一（缺规则 → 缺口，剔除）
        try:
            fp = rulebook.map_fiscal(
                d.party_id or "", d.fiscal_year_label, d.period_start, d.period_end
            )
            line.period_key = fp.period_key
            line.region = rulebook.normalize_region(d.region)
            gross, fx_info = rulebook.convert(d.amount, d.currency, on_day)
            line.anchor_amount_gross = round(gross, 2)
            line.fx = fx_info
            if d.partner_share_amount:
                ps_amt, _ = rulebook.convert(d.partner_share_amount, d.currency, on_day)
            else:
                ps_amt = 0.0
        except Exception as exc:  # RuleGapError
            line.reasons.append(R_RULE_GAP)
            result.gaps.append({
                "disclosure_id": d.id,
                "kind": "rule-gap",
                "detail": str(exc),
            })
            lines[d.id] = line
            continue

        # 4) 权利金：让渡权利后取得，不重复计入全球产品销售
        if d.revenue_kind == REV_ROYALTY:
            line.reasons.append(R_ROYALTY)
            lines[d.id] = line
            continue

        # 5) 代收合作方份额：从产品销售中扣除（合作方自行在其区域确认）
        included = gross
        if d.includes_partner_share and ps_amt:
            included -= ps_amt
            line.adjustments.append({
                "type": R_PARTNER_SHARE,
                "counterparty_party_id": d.partner_party_id,
                "removed_anchor": round(ps_amt, 2),
            })
        if d.revenue_kind == REV_PARTNER_SHARE:
            # 整条都是代收份额 → 不计本主体
            line.reasons.append(R_PARTNER_SHARE)
            lines[d.id] = line
            continue

        line.anchor_amount_included = round(included, 2)
        line.status = ST_PARTIAL if line.adjustments else ST_INCLUDED
        lines[d.id] = line

    # ---- 同(地域,期间)去重 / 冲突识别 ----
    d_by_id = {d.id: d for d in disclosures}
    buckets: Dict[Tuple[str, str], List[SalesLine]] = {}
    for line in lines.values():
        if line.status not in (ST_INCLUDED, ST_PARTIAL):
            continue
        buckets.setdefault((line.region or "?", line.period_key or "?"), []).append(line)

    for (region, period), bucket in buckets.items():
        on_day = _bucket_day(bucket, d_by_id)
        parties = sorted({l.party_id for l in bucket})
        if len(parties) <= 1:
            continue  # 单一权利方，无重复

        # 1) 许可/转手：在该地域、该时点把权利让渡给桶内另一方的一方，
        #    只应确认权利金，其产品销售整条剔除。
        grantors = set()
        for a in parties:
            for b in parties:
                if a != b and rights.is_royalty_counterparty(a, b, region, on_day):
                    grantors.add(a)
        for line in bucket:
            if line.party_id in grantors and line.status in (ST_INCLUDED, ST_PARTIAL):
                line.status = ST_EXCLUDED
                if R_ROYALTY not in line.reasons:
                    line.reasons.append(R_ROYALTY)

        # 2) 仍在同一地域/期间按产品销售确认的多方，权利链无法拆成不重叠的
        #    独占权利 → 归属不清。区分共同开发双计与完全无链两种冲突，
        #    一律挂"待确认证据"，在采纳归属意见前不计入。
        remaining = [l for l in bucket if l.status in (ST_INCLUDED, ST_PARTIAL)]
        remaining_parties = sorted({l.party_id for l in remaining})
        if len(remaining_parties) > 1:
            all_codev = all(
                rights.codeveloped(a, b, region, on_day)
                or rights.codeveloped(b, a, region, on_day)
                for i, a in enumerate(remaining_parties)
                for b in remaining_parties[i + 1:]
            )
            conflict_type = (
                "codevelopment-double-book" if all_codev
                else "unexplained-multiparty-booked-sales"
            )
            ids = [l.disclosure_id for l in remaining]
            for line in remaining:
                line.status = ST_EXCLUDED
                if R_CONFLICT not in line.reasons:
                    line.reasons.append(R_CONFLICT)
            result.conflicts.append({
                "type": conflict_type,
                "region": region,
                "period_key": period,
                "parties": remaining_parties,
                "disclosure_ids": ids,
                "resolution": "pending-adopted-opinion",
            })
            result.gaps.append({
                "kind": "attribution-conflict",
                "conflict_type": conflict_type,
                "region": region,
                "period_key": period,
                "disclosure_ids": ids,
                "detail": "同一地域/期间多方确认产品销售且权利链无法拆分独占归属，"
                          "在采纳归属意见前不计入",
            })

    # ---- 汇总（去重后全球销售）----
    total = 0.0
    for line in lines.values():
        if line.status not in (ST_INCLUDED, ST_PARTIAL):
            continue
        total += line.anchor_amount_included
        result.by_period[line.period_key or "?"] = round(
            result.by_period.get(line.period_key or "?", 0.0)
            + line.anchor_amount_included, 2)
        result.by_region[line.region or "?"] = round(
            result.by_region.get(line.region or "?", 0.0)
            + line.anchor_amount_included, 2)
    result.global_sales_anchor = round(total, 2)
    result.lines = sorted(lines.values(), key=lambda l: l.disclosure_id)
    return result


def _bucket_day(bucket: List[SalesLine], d_by_id: Dict[str, SalesDisclosure]) -> str:
    for line in bucket:
        dd = d_by_id.get(line.disclosure_id)
        if dd is not None:
            day = dd.period_end or dd.period_start
            if day:
                return day
    return ""
