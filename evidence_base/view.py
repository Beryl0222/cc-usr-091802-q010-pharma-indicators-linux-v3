"""按使用人密级对发布结论做脱敏。

发布总数是官方口径，可对外展示；但结论里回链的**原始证据内容**（尤其未公开
合同、受限来源）只能对获授权人员开放。脱敏保留聚合数字与"存在一条受限依据"
的事实，把受限记录的具体字段替换为占位，既不泄露合同，也不假装依据不存在。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from .store import Actor, EvidenceStore

_REDACTED = {"redacted": True, "reason": "restricted-record"}


def _is_restricted_for(store: EvidenceStore, record_id: str, actor: Optional[Actor]) -> bool:
    if actor is not None and actor.id == "system":
        return False
    rec = store.version_at(record_id)
    if rec is None:
        return False
    return not store.is_authorized(rec, actor)


def redact_determination(
    store: EvidenceStore,
    determination: Dict[str, Any],
    actor: Optional[Actor],
) -> Dict[str, Any]:
    """返回脱敏后的结论副本。系统主体或已获授权时原样返回。"""
    if actor is not None and actor.id == "system":
        return determination

    out = copy.deepcopy(determination)
    restricted_hits = []

    # 销售明细行：受限披露整行脱敏（保留其是否计入与金额口径的占位说明）
    for line in out.get("sales", {}).get("lines", []):
        rid = line.get("disclosure_id")
        if rid and _is_restricted_for(store, rid, actor):
            line["raw"] = dict(_REDACTED)
            line["fx"] = None
            line["source_id"] = None
            line["note_redacted"] = True
            restricted_hits.append(rid)

    # 首创依据回链：受限发现/批准只留 id 与"受限"标记
    fic = out.get("fic", {})
    safe_evidence = []
    for ev in fic.get("evidence", []):
        rid = ev.get("record_id")
        if rid and _is_restricted_for(store, rid, actor):
            safe_evidence.append({
                "record_id": rid,
                "kind": ev.get("kind"),
                "redacted": True,
                "reason": "restricted-record",
            })
            restricted_hits.append(rid)
        else:
            safe_evidence.append(ev)
    fic["evidence"] = safe_evidence

    redacted_opinions = []
    for op in fic.get("competing_opinions", []):
        # 竞争性意见本身若引用受限来源，对未授权者隐藏其逐字主张
        if op.get("opinion_id") and _is_restricted_for(store, op["opinion_id"], actor):
            redacted_opinions.append({
                "opinion_id": op["opinion_id"], "redacted": True,
                "reason": "restricted-record",
            })
        else:
            redacted_opinions.append(op)
    fic["competing_opinions"] = redacted_opinions

    out["_access"] = {
        "actor": actor.id if actor else "anonymous",
        "restricted_record_count": len(set(restricted_hits)),
        "note": "聚合发布数字可查；受限原始证据仅授权人员可见原文",
    }
    return out


def redact_snapshot(
    store: EvidenceStore, snapshot: Dict[str, Any], actor: Optional[Actor]
) -> Dict[str, Any]:
    out = copy.deepcopy(snapshot)
    out["determinations"] = [
        redact_determination(store, d, actor) for d in out.get("determinations", [])
    ]
    out["redacted"] = actor is None or actor.id != "system"
    return out
