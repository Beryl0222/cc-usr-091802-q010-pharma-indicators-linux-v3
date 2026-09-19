"""监测引擎：把证据、合并、首创认定组装为可发布的品种结论与指标。

核心约束：

* **没有手填汇总入口**。:class:`Determination` 的全球销售只能来自
  :func:`consolidate_sales` 的溯源结果；尝试注入手填总数直接抛
  :class:`OverrideGuardError`。每条结论都附来源版本与规则版本。
* **局部重算**。新版本（重述、批准日期更正、后到海外证据）只重算受影响品种，
  未受影响品种的结论字节级沿用，已发布快照永不改写。
* **复现**。按快照坐标（as_of + 规则版本 + 采纳意见）重建，与冻结指纹比对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .consolidation import consolidate_sales
from .fic import assess_fic
from .model import Candidate
from .rules import RuleBook
from .snapshot import Snapshot, freeze_snapshot
from .store import EvidenceStore

# 影响品种结论的证据类型（用于局部重算的 记录->品种 映射）
_CANDIDATE_BEARING = {
    "candidate", "indication", "keydiscovery", "approval",
    "relation", "salesdisclosure", "opinion", "review",
}


class OverrideGuardError(PermissionError):
    """尝试绕过来源链，手工填报汇总数。"""


@dataclass
class Period:
    """统计期定义。"""

    period_id: str
    label: str
    sales_period_key: str                 # 计入"全球年销售额"的归一期间
    rulebook_version: str
    blockbuster_threshold_usd: float = 1_000_000_000.0
    threshold_ref_date: Optional[str] = None  # 折算阈值所用基准日
    cohort_candidate_ids: Optional[List[str]] = None  # 显式分母；空=按批准全集


@dataclass
class Determination:
    """单个品种的发布结论：是否计入、首创依据、去重全球销售、规则、待确认项。"""

    candidate_id: str
    included: bool
    include_reasons: List[str]
    fic: Dict[str, Any]
    sales: Dict[str, Any]
    pending_evidence: List[Dict[str, Any]]
    conversion: Dict[str, Any]
    evidence_versions: Dict[str, int]
    totals_provenance: str = "derived-from-source-chain"  # 恒为派生，禁止手填

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "included": self.included,
            "include_reasons": self.include_reasons,
            "fic": self.fic,
            "sales": self.sales,
            "pending_evidence": self.pending_evidence,
            "conversion": self.conversion,
            "evidence_versions": self.evidence_versions,
            "totals_provenance": self.totals_provenance,
        }


class MonitoringEngine:
    def __init__(self, store: EvidenceStore, rulebook: RuleBook) -> None:
        self.store = store
        self.rulebook = rulebook

    # ------------------------------------------------------------ 工具
    def _candidates(self, as_of: Optional[str]) -> List[Candidate]:
        return list(self.store.iter_kind("candidate", as_of=as_of))

    @staticmethod
    def period_from_dict(data: Dict[str, Any]) -> "Period":
        return Period(
            period_id=data["period_id"],
            label=data.get("label", data["period_id"]),
            sales_period_key=data["sales_period_key"],
            rulebook_version=data["rulebook_version"],
            blockbuster_threshold_usd=data.get(
                "blockbuster_threshold_usd", 1_000_000_000.0),
            threshold_ref_date=data.get("threshold_ref_date"),
            cohort_candidate_ids=data.get("cohort_candidate_ids"),
        )

    def submit_manual_total(self, *_args, **_kwargs):
        """明确拒绝：监测人员不能用手填汇总数绕过来源链。"""
        raise OverrideGuardError(
            "汇总数必须由销售披露经去重、抵销、换算派生，不接受手填总数"
        )

    # ------------------------------------------------------ 记录->品种 映射
    def _candidates_touched_by(self, record_id: str, as_of: str) -> List[str]:
        rec_now = self.store.version_at(record_id, as_of)
        if rec_now is None:
            return []
        entity = rec_now.to_entity()
        kind = rec_now.kind
        touched: List[str] = []
        cid = getattr(entity, "candidate_id", None)
        if cid:
            touched.append(cid)
        if kind == "keydiscovery":
            for cand in self._candidates(as_of):
                if cand.compound_id == getattr(entity, "compound_id", None):
                    touched.append(cand.id)
        if kind == "candidate":
            touched.append(record_id)
        # 化合物改名/别名变化会影响其全部候选药
        if kind == "compound":
            for cand in self._candidates(as_of):
                if cand.compound_id == record_id:
                    touched.append(cand.id)
        return sorted(set(touched))

    # ------------------------------------------------------------ 品种结论
    def build_determination(
        self,
        candidate: Candidate,
        period: Period,
        as_of: str,
        adopted_opinion_id: Optional[str] = None,
    ) -> Determination:
        rb = self.rulebook.get(period.rulebook_version)

        fic = assess_fic(
            self.store, candidate, rb.version,
            as_of=as_of, adopted_opinion_id=adopted_opinion_id,
        )
        sales = consolidate_sales(self.store, candidate, rb, as_of=as_of)

        # 该统计期（年度）的去重全球销售
        annual = sales.by_period.get(period.sales_period_key, 0.0)

        # 十亿美元阈值按规则版本折算到锚币种
        threshold_anchor, threshold_fx = rb.convert(
            period.blockbuster_threshold_usd, "USD",
            period.threshold_ref_date or as_of,
        )
        threshold_anchor = round(threshold_anchor, 2)

        # 阻塞性销售缺口：该年度相关的归属冲突 / 换算缺口
        blocking_sales_gaps = [
            g for g in sales.gaps
            if g.get("period_key") in (None, period.sales_period_key)
        ]
        blockbuster = annual >= threshold_anchor and not blocking_sales_gaps

        include_reasons: List[str] = []
        if fic.counts_as_fic:
            include_reasons.append("counts-as-first-in-class")
        if blockbuster:
            include_reasons.append("global-annual-sales-over-1b-usd")
        included = bool(include_reasons)

        # 待确认证据：首创缺口 + 销售冲突/缺口（去重）
        pending: List[Dict[str, Any]] = []
        for g in fic.gaps:
            pending.append({"domain": "fic", **g})
        for g in sales.gaps:
            pending.append({"domain": "sales", **g})
        for c in sales.conflicts:
            pending.append({
                "domain": "sales",
                "code": "open-conflict",
                "conflict_type": c.get("type"),
                "region": c.get("region"),
                "period_key": c.get("period_key"),
                "disclosure_ids": c.get("disclosure_ids"),
            })

        # 该结论依赖的证据版本指纹（局部重算与溯源用）
        ev_versions: Dict[str, int] = {}
        for ref in fic.evidence:
            ev_versions[ref.record_id] = ref.version
        for line in sales.lines:
            rec = self.store.version_at(line.disclosure_id, as_of)
            if rec:
                ev_versions[line.disclosure_id] = rec.version
        for oid in (adopted_opinion_id,):
            rec = self.store.version_at(oid, as_of) if oid else None
            if rec:
                ev_versions[oid] = rec.version
        for review in (fic.reviews.get("science"), fic.reviews.get("stats")):
            if review:
                rec = self.store.version_at(review["review_id"], as_of)
                if rec:
                    ev_versions[review["review_id"]] = rec.version

        return Determination(
            candidate_id=candidate.id,
            included=included,
            include_reasons=include_reasons,
            fic={
                "counts_as_fic": fic.counts_as_fic,
                "status": fic.status,
                "adopted_opinion_id": adopted_opinion_id,
                "basis": fic.basis,
                "evidence": [e.to_dict() for e in fic.evidence],
                "competing_opinions": fic.competing_opinions,
                "reviews": fic.reviews,
                "gaps": fic.gaps,
            },
            sales={
                "global_annual_anchor": annual,
                "annual_period_key": period.sales_period_key,
                "global_sales_all_periods_anchor": sales.global_sales_anchor,
                "by_region": sales.by_region,
                "blockbuster_threshold_anchor": threshold_anchor,
                "blockbuster_threshold_fx": threshold_fx,
                "is_blockbuster": blockbuster,
                "rulebook_version": rb.version,
                "lines": [l.to_dict() for l in sales.lines],
                "eliminations": sales.eliminations,
                "conflicts": sales.conflicts,
                "gaps": sales.gaps,
            },
            pending_evidence=pending,
            conversion={
                "anchor_ccy": rb.anchor_ccy,
                "rulebook_version": rb.version,
                "fx_line_count": sum(1 for l in sales.lines if l.fx),
            },
            evidence_versions=dict(sorted(ev_versions.items())),
        )

    # ------------------------------------------------------------ 队列/指标
    def _cohort(self, period: Period, as_of: str) -> List[Candidate]:
        allc = self._candidates(as_of)
        if period.cohort_candidate_ids is not None:
            ids = set(period.cohort_candidate_ids)
            return [c for c in allc if c.id in ids]
        # 默认分母：统计期可见、至少有一个法域批准的新药
        approved = {
            a.candidate_id for a in self.store.iter_kind("approval", as_of=as_of)
        }
        return [c for c in allc if c.id in approved]

    def _metrics(
        self, period: Period, determinations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        cohort_n = len(determinations)
        fic_n = sum(1 for d in determinations if d["fic"]["counts_as_fic"])
        blockbusters = [
            d["candidate_id"] for d in determinations
            if d["sales"]["is_blockbuster"]
        ]
        return {
            "cohort_size": cohort_n,
            "first_in_class_count": fic_n,
            "fic_global_share": round(fic_n / cohort_n, 6) if cohort_n else None,
            "blockbuster_over_1b_usd_count": len(blockbusters),
            "blockbuster_candidate_ids": sorted(blockbusters),
            "sales_period_key": period.sales_period_key,
            "anchor_ccy": self.rulebook.get(period.rulebook_version).anchor_ccy,
        }

    def _global_fingerprint(self, as_of: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for rid in self.store.record_ids():
            rec = self.store.version_at(rid, as_of)
            if rec:
                out[rid] = rec.version
        return dict(sorted(out.items()))

    # ------------------------------------------------------------ 冻结
    def freeze_period(
        self,
        period: Period,
        as_of: str,
        adopted_opinions: Optional[Dict[str, Optional[str]]] = None,
        *,
        snapshot_id: str,
        frozen_at: str,
    ) -> Snapshot:
        adopted = adopted_opinions or {}
        cohort = self._cohort(period, as_of)
        determinations = [
            self.build_determination(
                c, period, as_of, adopted.get(c.id)
            ).to_dict()
            for c in cohort
        ]
        determinations.sort(key=lambda d: d["candidate_id"])
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            period_id=period.period_id,
            period_label=period.label,
            frozen_at=frozen_at,
            as_of=as_of,
            rulebook_version=period.rulebook_version,
            adopted_opinions=dict(sorted(
                (c.id, adopted.get(c.id)) for c in cohort
            )),
            determinations=determinations,
            metrics=self._metrics(period, determinations),
            evidence_fingerprint=self._global_fingerprint(as_of),
            redacted=False,
        )
        return freeze_snapshot(snapshot)

    # ------------------------------------------------------------ 局部重算
    def changed_records(self, old_as_of: str, new_as_of: str) -> List[Dict[str, Any]]:
        """对比两个时点，找出新增/升版的记录。"""
        changes: List[Dict[str, Any]] = []
        for rid in self.store.record_ids():
            old = self.store.version_at(rid, old_as_of)
            new = self.store.version_at(rid, new_as_of)
            if old is None and new is not None:
                changes.append({
                    "record_id": rid, "kind": new.kind,
                    "change": "new", "old_version": None,
                    "new_version": new.version,
                })
            elif old is not None and new is not None and old.version != new.version:
                changes.append({
                    "record_id": rid, "kind": new.kind,
                    "change": "new-version",
                    "old_version": old.version, "new_version": new.version,
                })
        return sorted(changes, key=lambda c: c["record_id"])

    def recompute_after_updates(
        self,
        previous: Snapshot,
        period: Period,
        new_as_of: str,
        adopted_opinions: Optional[Dict[str, Optional[str]]] = None,
        *,
        snapshot_id: str,
        frozen_at: str,
    ) -> Dict[str, Any]:
        """只重算受新版本影响的品种，其余字节级沿用。"""
        changes = self.changed_records(previous.as_of, new_as_of)

        affected: set = set()
        triggers: List[Dict[str, Any]] = []
        for ch in changes:
            if ch["kind"] not in _CANDIDATE_BEARING:
                continue
            touched = self._candidates_touched_by(ch["record_id"], new_as_of)
            for cid in touched:
                affected.add(cid)
                triggers.append({**ch, "affects_candidate_id": cid})
        # 采纳意见被指定/改变的品种也要重算
        adopted = adopted_opinions or dict(previous.adopted_opinions)
        for cid, oid in adopted.items():
            if previous.adopted_opinions.get(cid) != oid:
                affected.add(cid)

        prev_by_id = {d["candidate_id"]: d for d in previous.determinations}
        cohort = self._cohort(period, new_as_of)
        rebuilt: List[Dict[str, Any]] = []
        recomputed_ids: List[str] = []
        for c in cohort:
            if c.id in affected:
                rebuilt.append(self.build_determination(
                    c, period, new_as_of, adopted.get(c.id)
                ).to_dict())
                recomputed_ids.append(c.id)
            elif c.id in prev_by_id:
                rebuilt.append(prev_by_id[c.id])  # 字节级沿用
            else:
                rebuilt.append(self.build_determination(
                    c, period, new_as_of, adopted.get(c.id)
                ).to_dict())
                recomputed_ids.append(c.id)
        rebuilt.sort(key=lambda d: d["candidate_id"])

        new_snapshot = Snapshot(
            snapshot_id=snapshot_id,
            period_id=period.period_id,
            period_label=period.label,
            frozen_at=frozen_at,
            as_of=new_as_of,
            rulebook_version=period.rulebook_version,
            adopted_opinions=dict(sorted(
                (c.id, adopted.get(c.id)) for c in cohort
            )),
            determinations=rebuilt,
            metrics=self._metrics(period, rebuilt),
            evidence_fingerprint=self._global_fingerprint(new_as_of),
            redacted=False,
        )
        freeze_snapshot(new_snapshot)

        unchanged = [
            cid for cid in prev_by_id
            if cid not in recomputed_ids and cid in {c.id for c in cohort}
        ]
        return {
            "snapshot": new_snapshot,
            "recomputed_candidate_ids": sorted(recomputed_ids),
            "unchanged_candidate_ids": sorted(unchanged),
            "triggers": triggers,
            "changed_records": changes,
        }

    # ------------------------------------------------------------ 复现
    def reproduce(self, snapshot: Snapshot) -> Dict[str, Any]:
        """按快照坐标在当前库上重建并比对（证据被升版时会显式报差异）。"""
        period = Period(
            period_id=snapshot.period_id,
            label=snapshot.period_label,
            sales_period_key=snapshot.metrics.get("sales_period_key", ""),
            rulebook_version=snapshot.rulebook_version,
        )
        rebuilt = self.freeze_period(
            period, snapshot.as_of,
            adopted_opinions={k: v for k, v in snapshot.adopted_opinions.items()},
            snapshot_id=snapshot.snapshot_id,
            frozen_at=snapshot.frozen_at,
        )
        from .snapshot import reproduce_snapshot
        return reproduce_snapshot(snapshot, rebuilt)
