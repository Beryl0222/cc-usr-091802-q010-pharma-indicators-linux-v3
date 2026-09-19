"""冻结的发布快照。

FIC 结论经科学与统计两类复核后，在指定统计期冻结为不可变快照。快照钉住：

* 统计期与冻结时的系统时点 ``as_of``；
* 换算规则版本；
* 每个品种采纳的竞争性意见；
* 该时点全部证据的版本指纹（record_id -> version）。

因此即便事后企业重述年报、更正批准日期、补来海外证据（产生新版本），旧快照
仍可用旧时点 + 旧规则版本 + 旧采纳意见逐字复现。快照内容带 SHA-256 指纹，
任何对已发布数字的改动都会使指纹不一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"


@dataclass
class Snapshot:
    snapshot_id: str
    period_id: str
    period_label: str
    frozen_at: str
    as_of: str
    rulebook_version: str
    adopted_opinions: Dict[str, Optional[str]]
    determinations: List[Dict[str, Any]]
    metrics: Dict[str, Any]
    evidence_fingerprint: Dict[str, int]
    redacted: bool
    content_hash: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def canonical_payload(self) -> Dict[str, Any]:
        """不含 content_hash 自身的规范化内容（用于指纹与复现比对）。"""
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "period_id": self.period_id,
            "period_label": self.period_label,
            "as_of": self.as_of,
            "rulebook_version": self.rulebook_version,
            "adopted_opinions": self.adopted_opinions,
            "determinations": self.determinations,
            "metrics": self.metrics,
            "evidence_fingerprint": self.evidence_fingerprint,
            "redacted": self.redacted,
        }


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def freeze_snapshot(snapshot: Snapshot) -> Snapshot:
    """计算并写入内容指纹。冻结后调用方不应再改动其字段。"""
    snapshot.content_hash = compute_hash(snapshot.canonical_payload())
    return snapshot


def reproduce_snapshot(snapshot: Snapshot, rebuilt: Snapshot) -> Dict[str, Any]:
    """用当前库按快照坐标重建，再与原快照逐字段比对。"""
    old_hash = snapshot.content_hash
    rebuilt_hash = compute_hash(rebuilt.canonical_payload())
    mismatches: Dict[str, Any] = {}

    if snapshot.evidence_fingerprint != rebuilt.evidence_fingerprint:
        changed = {}
        keys = set(snapshot.evidence_fingerprint) | set(rebuilt.evidence_fingerprint)
        for k in sorted(keys):
            a = snapshot.evidence_fingerprint.get(k)
            b = rebuilt.evidence_fingerprint.get(k)
            if a != b:
                changed[k] = {"frozen": a, "rebuilt": b}
        mismatches["evidence_versions"] = changed

    old_det = {d["candidate_id"]: d for d in snapshot.determinations}
    new_det = {d["candidate_id"]: d for d in rebuilt.determinations}
    det_changed = {}
    for cid in sorted(set(old_det) | set(new_det)):
        if old_det.get(cid) != new_det.get(cid):
            det_changed[cid] = True
    if det_changed:
        mismatches["determinations"] = sorted(det_changed)
    if snapshot.metrics != rebuilt.metrics:
        mismatches["metrics"] = {"frozen": snapshot.metrics, "rebuilt": rebuilt.metrics}

    return {
        "reproduced": old_hash == rebuilt_hash and not mismatches,
        "frozen_hash": old_hash,
        "rebuilt_hash": rebuilt_hash,
        "hash_matches": old_hash == rebuilt_hash,
        "mismatches": mismatches,
    }
