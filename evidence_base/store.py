"""证据存储：仅追加、带版本、双时间、带来源链与访问控制。

* 每条证据按 id 保存一串版本；新版本永不覆盖旧版本。企业重述年报、批准日期
  更正、后到的海外证据都以新版本写入，触发局部重算，但历史快照仍引用旧版本，
  因此已发布年度数字可逐字复现。
* 双时间：业务时间在实体自身（批准日、生效区间、披露期间），系统时间是
  ``recorded_at``（版本进入系统的时刻）。``as_of`` 取该时刻"看得见"的最新版本。
* 来源链：事实类证据必须能解析到一个 :class:`Source`。
* 访问控制：未公开合同/内部材料按密级与授权名单限制，未获授权取不到原文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import (
    CLEARANCE_CONFIDENTIAL,
    CLEARANCE_PUBLIC,
    Entity,
    KIND,
    Restriction,
    Source,
    inflate,
)


class SecurityError(PermissionError):
    """访问未获授权的受限证据。"""


class ValidationError(ValueError):
    """证据缺少来源链或不满足入库约束。"""


@dataclass(frozen=True)
class Actor:
    """系统使用人：密级 + 显式授权。"""

    id: str
    name: str = ""
    clearance: int = CLEARANCE_PUBLIC
    authorized_record_ids: frozenset = field(default_factory=frozenset)

    @classmethod
    def system(cls) -> "Actor":
        return cls("system", "系统计算主体", CLEARANCE_CONFIDENTIAL)


@dataclass
class VersionedRecord:
    record_id: str
    kind: str
    version: int
    recorded_at: str
    actor_id: str
    data: Dict[str, Any]
    source_id: Optional[str]

    def to_entity(self) -> Entity:
        payload = dict(self.data)
        payload["kind"] = self.kind
        payload["id"] = self.record_id
        return inflate(payload)


# 必须有可解析来源的事实类档案（主体、化合物、候选药、适应证等主数据，
# 以及访问限制本身不强制外部来源）
_EVIDENTIARY = {
    "keydiscovery",
    "approval",
    "relation",
    "salesdisclosure",
    "opinion",
    "review",
}


class EvidenceStore:
    def __init__(self, strict_sources: bool = True) -> None:
        # record_id -> list[VersionedRecord]（按写入顺序，版本自增）
        self._versions: Dict[str, List[VersionedRecord]] = {}
        self._sources: Dict[str, Source] = {}
        self._restrictions: Dict[str, Restriction] = {}
        self.strict_sources = strict_sources

    # ------------------------------------------------------------------ 写入
    def put(
        self,
        entity: Entity,
        recorded_at: str,
        actor: Optional[Actor] = None,
    ) -> VersionedRecord:
        """写入证据的一个新版本（仅追加）。"""
        kind = KIND[type(entity)]
        payload = entity.to_dict()
        payload.pop("kind", None)

        if isinstance(entity, Source):
            self._sources[entity.id] = entity
        if isinstance(entity, Restriction):
            self._restrictions[entity.record_id] = entity
            # 限制规则本身不要求外部来源
        elif self.strict_sources and kind in _EVIDENTIARY:
            if not entity.source_id:
                raise ValidationError(f"{kind} {entity.id} 缺少来源链 source_id")
            if entity.source_id not in self._sources:
                raise ValidationError(
                    f"{kind} {entity.id} 的来源 {entity.source_id} 未登记"
                )

        history = self._versions.setdefault(entity.id, [])
        version_no = len(history) + 1
        record = VersionedRecord(
            record_id=entity.id,
            kind=kind,
            version=version_no,
            recorded_at=recorded_at,
            actor_id=actor.id if actor else "unknown",
            data=payload,
            source_id=entity.source_id,
        )
        history.append(record)
        return record

    # ------------------------------------------------------------ 可见版本
    def version_at(
        self, record_id: str, as_of: Optional[str] = None
    ) -> Optional[VersionedRecord]:
        history = self._versions.get(record_id)
        if not history:
            return None
        visible = [
            v for v in history if as_of is None or v.recorded_at <= as_of
        ]
        if not visible:
            return None
        return visible[-1]

    def get(
        self,
        record_id: str,
        *,
        as_of: Optional[str] = None,
        actor: Optional[Actor] = None,
    ) -> Optional[Entity]:
        record = self.version_at(record_id, as_of)
        if record is None:
            return None
        self.authorize(record, actor)
        return record.to_entity()

    def history(self, record_id: str) -> List[VersionedRecord]:
        return list(self._versions.get(record_id, ()))

    def iter_kind(
        self,
        kind: str,
        *,
        as_of: Optional[str] = None,
        actor: Optional[Actor] = None,
        include_restricted: bool = False,
    ) -> List[Entity]:
        """取某类证据在 as_of 时刻的最新版本。

        默认对 ``actor`` 不可见的受限记录直接跳过；``include_restricted``
        时返回其 :class:`VersionedRecord` 头（数据脱敏），供统计"仍有受限缺口"。
        """
        out: List[Entity] = []
        for rid in self._versions:
            rec = self.version_at(rid, as_of)
            if rec is None or rec.kind != kind:
                continue
            if not self.is_authorized(rec, actor):
                if include_restricted:
                    out.append(_Redacted(rid, kind, rec.recorded_at))  # type: ignore
                continue
            out.append(rec.to_entity())
        return out

    def all_current(self, as_of: Optional[str] = None) -> List[Entity]:
        return [
            rec.to_entity()
            for rec in (self.version_at(rid, as_of) for rid in self._versions)
            if rec is not None
        ]

    def record_ids(self, kind: Optional[str] = None) -> List[str]:
        if kind is None:
            return list(self._versions)
        return [
            rid for rid in self._versions
            if (self.version_at(rid) and self.version_at(rid).kind == kind)
        ]

    # ------------------------------------------------------------ 访问控制
    def is_authorized(
        self, record: VersionedRecord, actor: Optional[Actor]
    ) -> bool:
        required = self.required_clearance(record)
        restriction = self._restrictions.get(record.record_id)
        # 系统计算主体始终可见（用于生成发布数）；个人访问按密级+名单
        if actor is not None and actor.id == "system":
            return True
        if actor is None:
            return required == CLEARANCE_PUBLIC
        if restriction is not None and restriction.allowed_actors:
            if actor.id not in restriction.allowed_actors:
                return False
        if record.record_id in getattr(actor, "authorized_record_ids", frozenset()):
            return True
        return actor.clearance >= required

    def authorize(
        self, record: VersionedRecord, actor: Optional[Actor]
    ) -> None:
        if not self.is_authorized(record, actor):
            raise SecurityError(
                f"证据 {record.record_id} 需要更高密级或显式授权"
            )

    def required_clearance(self, record: VersionedRecord) -> int:
        level = CLEARANCE_PUBLIC
        source = self._sources.get(record.source_id) if record.source_id else None
        if source is not None and source.confidential:
            level = max(level, source.clearance_required)
        restriction = self._restrictions.get(record.record_id)
        if restriction is not None:
            level = max(level, restriction.minimum_clearance)
        if record.data.get("confidential"):
            level = max(level, CLEARANCE_CONFIDENTIAL)
        return level

    def is_restricted(self, record_id: str) -> bool:
        rec = self.version_at(record_id)
        return rec is not None and self.required_clearance(rec) > CLEARANCE_PUBLIC

    # ------------------------------------------------------------ 工具
    def source_of(self, record: VersionedRecord) -> Optional[Source]:
        return self._sources.get(record.source_id) if record.source_id else None

    def has_record(self, record_id: str) -> bool:
        return record_id in self._versions


class _Redacted:
    """受限记录的脱敏占位（只暴露 id/类型/记录时间，不含任何业务数据）。"""

    def __init__(self, record_id: str, kind: str, recorded_at: str) -> None:
        self.id = record_id
        self.source_id = None
        self.kind = kind
        self.recorded_at = recorded_at
        self.redacted = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "redacted": True,
            "recorded_at": self.recorded_at,
        }
