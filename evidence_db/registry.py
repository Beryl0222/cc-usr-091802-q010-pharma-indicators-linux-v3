"""只追加的证据登记册：版本链、血缘、局部重算与期间冻结。

关键设计
--------
1. **事件序列**：每条证据的每个版本、每版换算规则、每次复核都是一个
   只追加事件，带递增 ``seq``。任何历史状态都可通过
   ``view_as_of(seq)`` 精确重建。
2. **版本而非覆盖**：企业重述年报、批准日期更正、后到的海外证据都写入
   ``supersedes`` 旧版本的新记录；旧记录保留以复现旧结论。
3. **血缘与局部重算**：品种结论记录所依赖的全部记录键。新事件只使
   关联品种的结论失效，其它品种与已冻结年度不动。
4. **冻结快照**：FIC 结论经两类复核后在指定统计期冻结，快照含输入指纹
   清单；可用 ``verify_freeze`` 重算比对，保证已发布年度数字逐位复现。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from . import evidence as ev
from .rules import RuleBookVersion, VersionedRuleBook
from .timeline import StatsPeriod


class RegistryError(Exception):
    """登记/版本链错误。"""


class SourceChainViolation(Exception):
    """试图在没有来源链支撑的情况下形成或写入汇总数字。"""


# ---------------------------------------------------------------------------
# 事件与视图
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    seq: int
    kind: str            # record | rulebook | review
    key: str             # 稳定键（类型:id 或规则版本号）
    version: int
    digest: str
    recorded_at: str


@dataclass(frozen=True)
class EvidenceView:
    """某个事件序号下的证据快照（只含该时点的最新版本）。"""

    seq: int
    records: dict[str, list[ev.Record]]  # key(类型:id) -> 全部版本（按版本号）
    rules: VersionedRuleBook
    review_digests: dict[str, str]       # review id -> digest

    def latest(self, key: str) -> Optional[ev.Record]:
        versions = self.records.get(key)
        return versions[-1] if versions else None

    def all_latest(self, record_type: type) -> list[ev.Record]:
        out = []
        for versions in self.records.values():
            rec = versions[-1]
            if isinstance(rec, record_type):
                out.append(rec)
        return out

    def by_id(self, record_type: type, rec_id: str) -> Optional[ev.Record]:
        rec = self.latest(f"{record_type.__name__}:{rec_id}")
        return rec if isinstance(rec, record_type) else None


def _key(record: ev.Record) -> str:
    return f"{type(record).__name__}:{record.id}"


# ---------------------------------------------------------------------------
# 冻结快照
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrozenConclusion:
    candidate_id: str
    payload: dict                 # 引擎产出的完整结论
    digest: str                   # 结论规范化指纹


@dataclass(frozen=True)
class FreezeSnapshot:
    freeze_id: str
    period_code: str
    frozen_at: str
    event_seq: int
    rule_versions: dict[str, str]          # 披露日 -> 实际用到的规则版本
    conclusions: dict[str, FrozenConclusion]
    indicators: dict[str, float]
    manifest: dict                         # 输入记录指纹清单与复核指纹
    digest: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# 登记册
# ---------------------------------------------------------------------------

class Registry:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._records: dict[str, list[ev.Record]] = {}
        self._rules = VersionedRuleBook()
        self._rule_seq: dict[str, int] = {}
        self._reviews: dict[str, object] = {}
        self._freezes: dict[tuple[str, str], FreezeSnapshot] = {}
        # 血缘：period -> candidate -> set(记录键 / 规则键 / 复核键)
        self._lineage: dict[str, dict[str, set[str]]] = {}
        # 已冻结的品种结论，新证据不再就地改写
        self._frozen_candidates: dict[tuple[str, str], str] = {}

    # ----- 追加 -----------------------------------------------------------

    def add(self, record: ev.Record) -> Event:
        """追加一条证据记录（新版本）。

        销售披露等可计入汇总的记录必须携带来源文档，否则视为试图绕过
        来源链，直接拒绝。
        """
        self._require_traceable(record)
        key = _key(record)
        versions = self._records.setdefault(key, [])

        if versions:
            if record.version != versions[-1].version + 1:
                raise RegistryError(
                    f"{key} 新版本号须为 {versions[-1].version + 1}，收到 {record.version}"
                )
            if not record.supersedes or record.supersedes != versions[-1].version_id:
                raise RegistryError(
                    f"{key} v{record.version} 必须以 supersedes 指向上一版本 "
                    f"{versions[-1].version_id}"
                )
        elif record.version != 1:
            raise RegistryError(f"{key} 首版版本号必须为 1")
        if any(r.version_id == record.version_id for r in versions):
            raise RegistryError(f"{key} 版本标识重复: {record.version_id}")

        versions.append(record)
        return self._emit("record", key, record.version, record.digest(), record.recorded_at)

    def add_rulebook(self, book: RuleBookVersion, added_on: str) -> Event:
        if book.version in self._rule_seq:
            raise RegistryError(f"规则版本已存在: {book.version}")
        self._rules.add(book)
        evt = self._emit("rulebook", f"RuleBook:{book.version}", 1, book.digest(), added_on)
        self._rule_seq[book.version] = evt.seq
        return evt

    def add_review(self, review: object) -> Event:
        rid = getattr(review, "review_id")
        if rid in self._reviews:
            raise RegistryError(f"复核已存在: {rid}")
        self._reviews[rid] = review
        digest = review.digest()
        return self._emit(
            "review",
            f"Review:{rid}",
            1,
            digest,
            getattr(review, "reviewed_at"),
        )

    def _emit(self, kind: str, key: str, version: int, digest: str, when: str) -> Event:
        evt = Event(seq=len(self._events) + 1, kind=kind, key=key,
                    version=version, digest=digest, recorded_at=when)
        self._events.append(evt)
        return evt

    @staticmethod
    def _require_traceable(record: ev.Record) -> None:
        if isinstance(record, ev.SalesDisclosure):
            if record.source is None:
                raise SourceChainViolation(
                    f"销售披露 {record.id} 缺少来源文档，不能进入汇总来源链"
                )
            if record.amount and not record.stream_key:
                raise SourceChainViolation(
                    f"销售披露 {record.id} 缺少销售流标识 stream_key，无法去重/抵销"
                )

    # ----- 查询 -----------------------------------------------------------

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    @property
    def head_seq(self) -> int:
        return len(self._events)

    def records_of(self, key: str) -> list[ev.Record]:
        return list(self._records.get(key, ()))

    def rulebook(self) -> VersionedRuleBook:
        return self._rules

    def review(self, review_id: str):
        return self._reviews.get(review_id)

    def view_as_of(self, seq: Optional[int] = None) -> EvidenceView:
        """重建 ``seq`` 时点的证据视图；缺省为当前最新。"""
        cutoff = self.head_seq if seq is None else seq
        records: dict[str, list[ev.Record]] = {}
        for evt in self._events[:cutoff]:
            if evt.kind != "record":
                continue
            versions = self._records[evt.key]
            # 选出在该事件序号上已写入、且版本号 <= 事件版本的记录
            visible = [r for r in versions if r.version <= evt.version]
            if visible:
                records[evt.key] = visible
        rules = VersionedRuleBook()
        for evt in self._events[:cutoff]:
            if evt.kind == "rulebook":
                rules.add(self._rules.get(evt.key.split(":", 1)[1]))
        reviews = {
            evt.key.split(":", 1)[1]: evt.digest
            for evt in self._events[:cutoff]
            if evt.kind == "review"
        }
        return EvidenceView(seq=cutoff, records=records, rules=rules, review_digests=reviews)

    # ----- 血缘与局部重算 -------------------------------------------------

    def remember_lineage(self, period_code: str, candidate_id: str, deps: set[str]) -> None:
        self._lineage.setdefault(period_code, {}).setdefault(candidate_id, set()).update(deps)

    def affected_candidates(self, event: Event, period_code: str) -> set[str]:
        """新事件影响哪些品种（仅这些品种需要局部重算）。"""
        result: set[str] = set()
        lineage = self._lineage.get(period_code, {})
        for cand, deps in lineage.items():
            if event.key in deps:
                result.add(cand)
        # 未建立血缘的新品种（首次计算）也纳入
        if not result and event.kind == "record":
            rec = self._records[event.key][-1]
            linked = self._candidate_of(rec)
            if linked:
                result.add(linked)
        return result

    def _candidate_of(self, record: ev.Record) -> Optional[str]:
        if isinstance(record, (ev.Candidate, ev.SalesDisclosure, ev.Approval,
                               ev.FirstDiscovery, ev.LicenseRelation)):
            return record.candidate_id or None
        if isinstance(record, ev.Compound):
            for cand in self.view_as_of().all_latest(ev.Candidate):
                if cand.compound_id == record.id:
                    return cand.id
        if isinstance(record, ev.Indication):
            view = self.view_as_of()
            for r in view.all_latest(ev.Approval):
                if r.indication_id == record.id:
                    return r.candidate_id
        if isinstance(record, ev.GroupRelation):
            view = self.view_as_of()
            units = {record.unit}
            for d in view.all_latest(ev.SalesDisclosure):
                if d.reporting_company in units or d.counterparty in units:
                    return d.candidate_id
        if isinstance(record, (ev.AttributionClaim, ev.EvidenceGap)):
            subject = record.subject_id
            if subject.startswith("Candidate:"):
                return subject.split(":", 1)[1]
        return None

    # ----- 冻结 -----------------------------------------------------------

    def is_frozen(self, period_code: str, candidate_id: str) -> bool:
        return (period_code, candidate_id) in self._frozen_candidates

    def freeze(
        self,
        period: StatsPeriod,
        frozen_at: str,
        conclusions: dict[str, dict],
        indicators: dict[str, float],
        rule_versions: dict[str, str],
        review_ids: list[str],
    ) -> FreezeSnapshot:
        """把统计期结论冻结为不可变快照。

        每个被计入的销售数字必须能回溯到披露记录，否则拒绝冻结——
        已发布数字绝不允许来源链之外的手填汇总。
        """
        frozen = {}
        manifest_records: dict[str, str] = {}
        for cand_id, payload in conclusions.items():
            self._enforce_sales_provenance(cand_id, payload)
            digest = _payload_digest(payload)
            frozen[cand_id] = FrozenConclusion(cand_id, payload, digest)
            for key, dg in payload.get("input_digests", {}).items():
                manifest_records[key] = dg

        manifest = {
            "records": manifest_records,
            "rule_versions": {v: self._rules.get(v).digest() for v in set(rule_versions.values())},
            "reviews": {rid: self._events_lookup_review_digest(rid) for rid in review_ids},
            "head_seq": self.head_seq,
        }
        snapshot = FreezeSnapshot(
            freeze_id=f"freeze-{period.code}-{self.head_seq}",
            period_code=period.code,
            frozen_at=frozen_at,
            event_seq=self.head_seq,
            rule_versions=rule_versions,
            conclusions=frozen,
            indicators=dict(indicators),
            manifest=manifest,
            digest="",
        )
        object.__setattr__(
            snapshot,
            "digest",
            hashlib.sha256(
                json.dumps(
                    {
                        "period": snapshot.period_code,
                        "seq": snapshot.event_seq,
                        "conclusions": {k: v.digest for k, v in frozen.items()},
                        "indicators": snapshot.indicators,
                        "manifest": snapshot.manifest,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:16],
        )
        self._freezes[(period.code, snapshot.freeze_id)] = snapshot
        for cand_id in conclusions:
            self._frozen_candidates[(period.code, cand_id)] = snapshot.freeze_id
        return snapshot

    @staticmethod
    def _enforce_sales_provenance(cand_id: str, payload: dict) -> None:
        if not payload.get("included", False):
            return
        legs = payload.get("sales_legs") or []
        if not legs:
            raise SourceChainViolation(
                f"品种 {cand_id} 被计入却没有任何销售披露来源腿，禁止冻结"
            )
        for leg in legs:
            if not leg.get("disclosure_ids"):
                raise SourceChainViolation(
                    f"品种 {cand_id} 存在无来源的销售腿 {leg.get('stream_key')}，禁止冻结"
                )

    def _events_lookup_review_digest(self, review_id: str) -> str:
        for evt in self._events:
            if evt.kind == "review" and evt.key == f"Review:{review_id}":
                return evt.digest
        raise RegistryError(f"复核不存在: {review_id}")

    def get_freeze(self, period_code: str, freeze_id: str) -> FreezeSnapshot:
        return self._freezes[(period_code, freeze_id)]

    def latest_freeze(self, period_code: str) -> Optional[FreezeSnapshot]:
        matches = [f for (p, _), f in self._freezes.items() if p == period_code]
        return max(matches, key=lambda f: f.event_seq) if matches else None

    def verify_freeze(self, snapshot: FreezeSnapshot, recompute_payloads) -> bool:
        """以快照事件序号重算并逐品种比对指纹。

        ``recompute_payloads(period_code, view)`` 由引擎提供，返回
        ``{candidate_id: payload}``。
        """
        view = self.view_as_of(snapshot.event_seq)
        redone = recompute_payloads(snapshot.period_code, view)
        if set(redone) != set(snapshot.conclusions):
            return False
        for cand, frozen in snapshot.conclusions.items():
            if _payload_digest(redone[cand]) != frozen.digest:
                return False
        return True


def _payload_digest(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
