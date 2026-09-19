"""只读应用服务：装载证据库、登记冻结快照、按使用人密级提供发布结论。

HTTP 层只做只读查询；冻结/重算属于监测办公室的发布动作，通过库 API 或 CLI
完成后把不可变快照登记进来。任何接口都不接受手填汇总数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .engine import MonitoringEngine
from .loader import Dataset
from .model import CLEARANCE_PUBLIC
from .snapshot import Snapshot
from .store import Actor, EvidenceStore
from .view import redact_snapshot


class NotFound(KeyError):
    pass


@dataclass
class RegisteredSnapshot:
    snapshot: Snapshot


class Service:
    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        self.engine: MonitoringEngine = dataset.engine
        self.store: EvidenceStore = dataset.store
        self._snapshots: Dict[str, Snapshot] = {}

    # ------------------------------------------------------------ 快照登记
    def register(self, snapshot: Snapshot) -> None:
        if snapshot.snapshot_id in self._snapshots:
            raise ValueError(f"快照已存在且不可变: {snapshot.snapshot_id}")
        self._snapshots[snapshot.snapshot_id] = snapshot

    def list_snapshots(self) -> List[Dict[str, Any]]:
        return [
            {
                "snapshot_id": s.snapshot_id,
                "period_id": s.period_id,
                "period_label": s.period_label,
                "frozen_at": s.frozen_at,
                "as_of": s.as_of,
                "rulebook_version": s.rulebook_version,
                "content_hash": s.content_hash,
            }
            for s in sorted(self._snapshots.values(), key=lambda s: s.frozen_at)
        ]

    def _get(self, snapshot_id: str) -> Snapshot:
        if snapshot_id not in self._snapshots:
            raise NotFound(f"快照不存在: {snapshot_id}")
        return self._snapshots[snapshot_id]

    # ------------------------------------------------------------ 发布动作
    def freeze(
        self,
        period_id: str,
        as_of: str,
        snapshot_id: str,
        frozen_at: str,
        adopted_opinions: Optional[Dict[str, Optional[str]]] = None,
    ) -> Snapshot:
        period = self.engine.period_from_dict(self.dataset.period(period_id))
        snap = self.engine.freeze_period(
            period, as_of, adopted_opinions=adopted_opinions,
            snapshot_id=snapshot_id, frozen_at=frozen_at,
        )
        self.register(snap)
        return snap

    def recompute(
        self,
        previous_id: str,
        period_id: str,
        new_as_of: str,
        snapshot_id: str,
        frozen_at: str,
        adopted_opinions: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        previous = self._get(previous_id)
        period = self.engine.period_from_dict(self.dataset.period(period_id))
        result = self.engine.recompute_after_updates(
            previous, period, new_as_of, adopted_opinions=adopted_opinions,
            snapshot_id=snapshot_id, frozen_at=frozen_at,
        )
        self.register(result["snapshot"])
        return {
            "snapshot_id": snapshot_id,
            "recomputed_candidate_ids": result["recomputed_candidate_ids"],
            "unchanged_candidate_ids": result["unchanged_candidate_ids"],
            "triggers": result["triggers"],
            "changed_records": result["changed_records"],
        }

    # ------------------------------------------------------------ 只读查询
    def actor_for(self, actor_id: Optional[str], clearance: Optional[int]) -> Actor:
        if not actor_id:
            return Actor("anonymous", clearance=CLEARANCE_PUBLIC)
        return Actor(actor_id, clearance=clearance if clearance is not None else CLEARANCE_PUBLIC)

    def get_snapshot(
        self, snapshot_id: str, actor: Optional[Actor]
    ) -> Dict[str, Any]:
        snap = self._get(snapshot_id)
        return redact_snapshot(self.store, _snapshot_dict(snap), actor)

    def get_metrics(self, snapshot_id: str) -> Dict[str, Any]:
        return dict(self._get(snapshot_id).metrics)

    def get_determination(
        self, snapshot_id: str, candidate_id: str, actor: Optional[Actor]
    ) -> Dict[str, Any]:
        snap = self._get(snapshot_id)
        for d in snap.determinations:
            if d["candidate_id"] == candidate_id:
                from .view import redact_determination
                return redact_determination(self.store, d, actor)
        raise NotFound(f"快照 {snapshot_id} 中无品种 {candidate_id}")

    def reproduce(self, snapshot_id: str) -> Dict[str, Any]:
        return self.engine.reproduce(self._get(snapshot_id))

    def get_evidence(self, record_id: str, actor: Optional[Actor]) -> Dict[str, Any]:
        entity = self.store.get(record_id, actor=actor)
        if entity is None:
            raise NotFound(record_id)
        return entity.to_dict()

    def periods(self) -> List[Dict[str, Any]]:
        return list(self.dataset.periods)


def _snapshot_dict(snap: Snapshot) -> Dict[str, Any]:
    return {
        "snapshot_id": snap.snapshot_id,
        "period_id": snap.period_id,
        "period_label": snap.period_label,
        "frozen_at": snap.frozen_at,
        "as_of": snap.as_of,
        "rulebook_version": snap.rulebook_version,
        "adopted_opinions": snap.adopted_opinions,
        "determinations": snap.determinations,
        "metrics": snap.metrics,
        "evidence_fingerprint": snap.evidence_fingerprint,
        "redacted": snap.redacted,
        "content_hash": snap.content_hash,
    }
