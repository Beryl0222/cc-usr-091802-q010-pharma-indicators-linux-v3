"""首创（First-in-Class, FIC）认定。

谁算首创不靠单一字段拍板，而靠一组证据 + 竞争性意见 + 双重复核：

* 科学依据：首次关键发现（新靶点/新机制/新结构类型）、首创方、各法域首批。
* 来源冲突：并列登记竞争性 :class:`Opinion`，不预先合并；由复核择一"采纳"。
* 缺口：首创依据不足、存在未采纳的对立意见、缺少科学/统计复核等，都显式列出。
* 冻结：采纳意见且科学、统计两类复核均通过，才具备冻结资格；真正的冻结由
  :mod:`evidence_base.snapshot` / :mod:`evidence_base.engine` 钉住时点与规则版本。

认定本身是纯函数：给定 as_of 时点可见证据与被采纳意见 id，给出可复现结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import (
    REVIEW_SCIENCE,
    REVIEW_STATS,
    SUBJECT_FIC,
    Approval,
    Candidate,
    KeyDiscovery,
    Opinion,
    Review,
)
from .store import Actor, EvidenceStore

SYSTEM = Actor.system()

# 认定状态
FIC_INSUFFICIENT = "insufficient-evidence"   # 关键首创证据缺失，不能认定
FIC_CONTESTED = "contested"                  # 存在对立意见且未采纳
FIC_PENDING_REVIEW = "pending-review"        # 依据齐备/已采纳，但复核未齐
FIC_READY = "ready-to-freeze"                # 已采纳 + 双复核通过，可冻结
FIC_NOT_FIC = "not-first-in-class"           # 采纳意见认定其并非首创

# 缺口类型
GAP_NO_DISCOVERY = "missing-key-discovery"
GAP_NOVELTY = "novelty-unsubstantiated"
GAP_NO_APPROVAL = "no-jurisdiction-approval"
GAP_OPINIONS = "unresolved-competing-opinions"
GAP_SCIENCE_REVIEW = "missing-science-review"
GAP_STATS_REVIEW = "missing-stats-review"
GAP_ADOPTED_NOT_FOUND = "adopted-opinion-not-found"
GAP_RESTRICTED = "restricted-fic-evidence"


@dataclass
class EvidenceRef:
    record_id: str
    kind: str
    version: int
    source_id: Optional[str]
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "version": self.version,
            "source_id": self.source_id,
            "summary": self.summary,
        }


@dataclass
class FICAssessment:
    candidate_id: str
    as_of: Optional[str]
    rulebook_version: str
    adopted_opinion_id: Optional[str]
    status: str
    counts_as_fic: bool
    basis: Dict[str, Any]
    competing_opinions: List[Dict[str, Any]]
    reviews: Dict[str, Any]
    gaps: List[Dict[str, Any]]
    evidence: List[EvidenceRef]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "as_of": self.as_of,
            "rulebook_version": self.rulebook_version,
            "adopted_opinion_id": self.adopted_opinion_id,
            "status": self.status,
            "counts_as_fic": self.counts_as_fic,
            "basis": self.basis,
            "competing_opinions": self.competing_opinions,
            "reviews": self.reviews,
            "gaps": self.gaps,
            "evidence": [e.to_dict() for e in self.evidence],
        }


FICStatus = str


def _ref(store: EvidenceStore, entity: Any, summary: Dict[str, Any],
         as_of: Optional[str] = None) -> EvidenceRef:
    rec = store.version_at(entity.id, as_of)
    return EvidenceRef(
        record_id=entity.id,
        kind=rec.kind if rec else type(entity).__name__.lower(),
        version=rec.version if rec else 0,
        source_id=entity.source_id,
        summary=summary,
    )


def assess_fic(
    store: EvidenceStore,
    candidate: Candidate,
    rulebook_version: str,
    *,
    as_of: Optional[str] = None,
    adopted_opinion_id: Optional[str] = None,
    actor: Optional[Actor] = SYSTEM,
) -> FICAssessment:
    cid = candidate.id

    def _visible(kind):
        return store.iter_kind(kind, as_of=as_of, actor=actor)

    discoveries: List[KeyDiscovery] = [
        d for d in _visible("keydiscovery")
        if getattr(d, "redacted", False) is False
        and (d.candidate_id == cid or d.compound_id == candidate.compound_id)
    ]
    approvals: List[Approval] = [
        a for a in _visible("approval")
        if getattr(a, "redacted", False) is False and a.candidate_id == cid
    ]
    opinions: List[Opinion] = [
        o for o in _visible("opinion")
        if getattr(o, "redacted", False) is False
        and o.candidate_id == cid and o.subject == SUBJECT_FIC
    ]
    reviews: List[Review] = [
        r for r in _visible("review")
        if getattr(r, "redacted", False) is False and r.candidate_id == cid
    ]

    gaps: List[Dict[str, Any]] = []
    evidence: List[EvidenceRef] = []

    # ---- 科学依据：首次关键发现（取最早一条为首创锚点）----
    discoveries.sort(key=lambda d: d.discovered_on or "9999")
    novelty_substantiated = any(d.novelty_claim for d in discoveries)
    anchor_discovery = next(
        (d for d in discoveries if d.novelty_claim), discoveries[0] if discoveries else None
    )
    if not discoveries:
        gaps.append({"code": GAP_NO_DISCOVERY,
                     "detail": "缺少首次关键发现证据，首创无科学锚点"})
    else:
        if not novelty_substantiated:
            gaps.append({"code": GAP_NOVELTY,
                         "detail": "关键发现未载明新靶点/新机制/新结构类型主张"})
        for d in discoveries:
            evidence.append(_ref(store, d, {
                "title": d.title,
                "discovered_on": d.discovered_on,
                "party_id": d.party_id,
                "novelty_claim": d.novelty_claim,
            }, as_of))

    # ---- 各法域批准（每法域最早一次）----
    earliest_by_juris: Dict[str, Approval] = {}
    for a in approvals:
        cur = earliest_by_juris.get(a.jurisdiction)
        if cur is None or (a.decision_date or "9999") < (cur.decision_date or "9999"):
            earliest_by_juris[a.jurisdiction] = a
    if not approvals:
        gaps.append({"code": GAP_NO_APPROVAL,
                     "detail": "尚无任一法域批准，全球首创占比口径下暂不可计"})
    for juris, a in sorted(earliest_by_juris.items()):
        evidence.append(_ref(store, a, {
            "jurisdiction": juris,
            "decision_date": a.decision_date,
            "first_in_jurisdiction": a.first_in_jurisdiction_flag,
        }, as_of))

    # ---- 竞争性意见 ----
    competing = [{
        "opinion_id": o.id,
        "proponent": o.proponent,
        "claim": o.claim,
        "proposed": o.proposed,
        "confidence": o.confidence,
        "evidence_refs": o.evidence_refs,
        "version": (store.version_at(o.id).version if store.version_at(o.id) else 0),
    } for o in opinions]

    adopted: Optional[Opinion] = None
    if adopted_opinion_id is not None:
        adopted = next((o for o in opinions if o.id == adopted_opinion_id), None)
        if adopted is None:
            gaps.append({"code": GAP_ADOPTED_NOT_FOUND,
                         "detail": f"被采纳意见 {adopted_opinion_id} 在该时点不可见"})

    competing_claims = len({bool(o.proposed.get("is_first_in_class", True)) for o in opinions}) > 1
    if len(opinions) > 1 and adopted is None:
        gaps.append({"code": GAP_OPINIONS,
                     "detail": f"存在 {len(opinions)} 条竞争性首创意见，尚未择一采纳",
                     "opinion_ids": [o.id for o in opinions]})

    # ---- 科学 / 统计双重复核 ----
    # 无争议（未采纳特定意见）时接受面向品种的通用复核（opinion_id 为空）；
    # 一旦采纳了某条竞争性意见，复核必须明确针对该意见，不能用通用复核"盖章"。
    def review_ok(kind: str) -> Optional[Review]:
        for r in reviews:
            if r.review_type != kind or r.decision != "approved":
                continue
            required_opinion = adopted.id if adopted is not None else None
            if r.opinion_id != required_opinion:
                continue
            if r.rulebook_version not in (None, rulebook_version):
                continue
            return r
        return None

    science = review_ok(REVIEW_SCIENCE)
    stats = review_ok(REVIEW_STATS)
    if science is None:
        gaps.append({"code": GAP_SCIENCE_REVIEW,
                     "detail": "缺少针对采纳意见的科学复核通过记录"})
    if stats is None:
        gaps.append({"code": GAP_STATS_REVIEW,
                     "detail": f"缺少规则版本 {rulebook_version} 下的统计复核通过记录"})

    # ---- 受限证据提示（系统可见，但发布给个人时需脱敏）----
    restricted_ids = []
    for ev in evidence:
        rec = store.version_at(ev.record_id, as_of)
        if rec is not None and store.is_restricted(ev.record_id):
            restricted_ids.append(ev.record_id)
    if restricted_ids:
        gaps.append({"code": GAP_RESTRICTED,
                     "detail": "部分首创依据为未公开/受限材料，仅授权人员可见原文",
                     "record_ids": restricted_ids})

    # ---- 依据汇总 ----
    basis = {
        "originator_party_ids": list(candidate.originator_party_ids),
        "novelty_class": candidate.novelty_class,
        "anchor_discovery_id": anchor_discovery.id if anchor_discovery else None,
        "anchor_discovery_on": anchor_discovery.discovered_on if anchor_discovery else None,
        "anchor_party_id": anchor_discovery.party_id if anchor_discovery else None,
        "novelty_claim": anchor_discovery.novelty_claim if anchor_discovery else None,
        "first_jurisdictions": [
            j for j, a in earliest_by_juris.items() if a.first_in_jurisdiction_flag
        ],
        "earliest_approval": min(
            (a.decision_date for a in approvals if a.decision_date), default=None
        ),
    }
    if adopted is not None:
        basis["adopted_claim"] = adopted.claim
        basis["adopted_proponent"] = adopted.proponent

    # ---- 状态判定 ----
    blocking = {GAP_NO_DISCOVERY, GAP_NOVELTY, GAP_NO_APPROVAL, GAP_ADOPTED_NOT_FOUND}
    has_blocking_gap = any(g["code"] in blocking for g in gaps)

    if adopted is not None and adopted.proposed.get("is_first_in_class") is False:
        status = FIC_NOT_FIC
        counts = False
    elif has_blocking_gap:
        status = FIC_INSUFFICIENT
        counts = False
    elif adopted is None and (len(opinions) > 1 or competing_claims):
        status = FIC_CONTESTED
        counts = False
    elif science is not None and stats is not None:
        # 无争议时无须"采纳意见"；有竞争意见时采纳意见已在上一步消解争议
        status = FIC_READY
        counts = True
    else:
        status = FIC_PENDING_REVIEW
        counts = False

    return FICAssessment(
        candidate_id=cid,
        as_of=as_of,
        rulebook_version=rulebook_version,
        adopted_opinion_id=adopted_opinion_id,
        status=status,
        counts_as_fic=counts,
        basis=basis,
        competing_opinions=competing,
        reviews={
            "science": _review_dict(science),
            "stats": _review_dict(stats),
        },
        gaps=gaps,
        evidence=evidence,
    )


def _review_dict(r: Optional[Review]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    return {
        "review_id": r.id,
        "reviewer": r.reviewer,
        "decision": r.decision,
        "opinion_id": r.opinion_id,
        "rulebook_version": r.rulebook_version,
        "comment": r.comment,
    }
