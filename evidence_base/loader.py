"""数据集加载：按档案类型分文件读取，并支持带时间戳的事件回放。

目录约定（每类证据一个文件，体现"分别建档"）::

    parties.json  sources.json  compounds.json  candidates.json
    indications.json  discoveries.json  approvals.json
    relations.json  sales.json  opinions.json  reviews.json
    restrictions.json
    rulebooks.json      # 有版本的换算规则（汇率/财年/地域）
    periods.json        # 统计期定义
    events.json         # 可选：带 recorded_at 的写入事件，用于回放迟报/重述/更正

普通文件里每条记录可带 ``recorded_at`` / ``actor_id``；events.json 里同一 id
可出现多次，按 ``recorded_at`` 顺序回放即产生新版本（重述、批准日期更正、
后到海外证据都走这条路径），从而驱动局部重算，而旧快照依旧可复现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import CLEARANCE_PUBLIC, inflate
from .rules import FiscalPeriod, FXRate, RuleBook, RuleBookVersion
from .store import Actor, EvidenceStore

# 档案文件 -> 无（kind 已在记录内）
ENTITY_FILES = [
    "parties", "sources", "compounds", "candidates", "indications",
    "discoveries", "approvals", "relations", "sales", "opinions",
    "reviews", "restrictions",
]
DEFAULT_RECORDED_AT = "2026-01-01T00:00:00Z"

# 档案文件名 -> 实体 kind
_SINGULAR_KIND: Dict[str, str] = {
    "parties": "party",
    "sources": "source",
    "compounds": "compound",
    "candidates": "candidate",
    "indications": "indication",
    "discoveries": "keydiscovery",
    "approvals": "approval",
    "relations": "relation",
    "sales": "salesdisclosure",
    "opinions": "opinion",
    "reviews": "review",
    "restrictions": "restriction",
}


@dataclass
class Dataset:
    store: EvidenceStore
    rulebook: RuleBook
    periods: List[Dict[str, Any]] = field(default_factory=list)
    engine: Optional[Any] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    def period(self, period_id: str) -> Dict[str, Any]:
        for p in self.periods:
            if p["period_id"] == period_id:
                return p
        raise KeyError(f"统计期不存在: {period_id}")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _records(obj: Any) -> List[Dict[str, Any]]:
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("records"), list):
        return obj["records"]
    raise ValueError("档案文件应为列表，或含 records 列表的对象")


def _load_rulebooks(obj: Any) -> RuleBook:
    book = RuleBook()
    if not obj:
        return book
    versions = obj["versions"] if isinstance(obj, dict) else obj
    for v in versions:
        fx = [
            FXRate(r["base_date"], r["source_ccy"], r["target_ccy"], r["rate"])
            for r in v.get("fx_rates", [])
        ]
        calendars: Dict[str, Dict[str, FiscalPeriod]] = {}
        for party_id, cal in v.get("fiscal_calendars", {}).items():
            calendars[party_id] = {
                label: FiscalPeriod(fp["period_key"], fp["start"], fp["end"])
                for label, fp in cal.items()
            }
        book.add_version(RuleBookVersion(
            version=v["version"],
            recorded_at=v["recorded_at"],
            anchor_ccy=v.get("anchor_ccy", "CNY"),
            fx_rates=fx,
            fiscal_calendars=calendars,
            region_aliases=v.get("region_aliases", {}),
            note=v.get("note"),
        ))
    return book


def _build_store(
    files: Dict[str, List[Dict[str, Any]]],
    events: List[Dict[str, Any]],
) -> EvidenceStore:
    store = EvidenceStore()

    # 先登记来源与主体，保证来源链可解析
    def take(entry: Dict[str, Any], actor: Optional[Actor], kind: Optional[str] = None):
        if kind:
            entry["kind"] = kind
        if "kind" not in entry:
            raise ValueError(f"记录缺少 kind：{entry.get('id')}")
        recorded_at = entry.pop("recorded_at", DEFAULT_RECORDED_AT)
        actor_id = entry.pop("actor_id", None)
        entity = inflate(entry)
        act = actor or Actor(actor_id or "loader", clearance=CLEARANCE_PUBLIC)
        store.put(entity, recorded_at, act)
        return recorded_at

    for entry in files.get("sources", []):
        take(entry, None, "source")
    for entry in files.get("parties", []):
        take(entry, None, "party")

    # 其余静态档案
    for name in ENTITY_FILES:
        if name in ("sources", "parties"):
            continue
        for entry in files.get(name, []):
            take(entry, None, _SINGULAR_KIND.get(name, name.rstrip("s")))

    # 事件回放（按时间）；同 id 多次 → 新版本
    for ev in sorted(events, key=lambda e: e.get("recorded_at", DEFAULT_RECORDED_AT)):
        payload = dict(ev["data"])
        if ev.get("kind"):
            payload["kind"] = ev["kind"]
        # 事件外层的 recorded_at / actor 才是该版本真正的入库元数据
        if "recorded_at" in ev:
            payload["recorded_at"] = ev["recorded_at"]
        actor = None
        if ev.get("actor_id"):
            actor = Actor(ev["actor_id"], clearance=ev.get("clearance", CLEARANCE_PUBLIC))
        take(payload, actor)

    return store


def load_dataset_dir(dir_path: str) -> Dataset:
    base = Path(dir_path)
    files: Dict[str, List[Dict[str, Any]]] = {}
    for name in ENTITY_FILES:
        files[name] = [dict(r) for r in _records(_read_json(base / f"{name}.json"))]

    rulebook = _load_rulebooks(_read_json(base / "rulebooks.json"))
    periods_obj = _read_json(base / "periods.json")
    periods = periods_obj.get("periods", []) if isinstance(periods_obj, dict) else (periods_obj or [])

    events_obj = _read_json(base / "events.json")
    events = _records(events_obj)

    store = _build_store(files, events)

    from .engine import MonitoringEngine
    ds = Dataset(store=store, rulebook=rulebook, periods=periods, events=events)
    ds.engine = MonitoringEngine(store, rulebook)
    return ds


def load_dataset(paths: Dict[str, str]) -> Dataset:
    """从显式给出的 文件路径->类型 映射加载（主要用于测试/临时数据）。"""
    files: Dict[str, List[Dict[str, Any]]] = {name: [] for name in ENTITY_FILES}
    rulebook = RuleBook()
    periods: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for path, kind in paths.items():
        obj = _read_json(Path(path))
        if kind == "rulebooks":
            rulebook = _load_rulebooks(obj)
        elif kind == "periods":
            periods = obj.get("periods", []) if isinstance(obj, dict) else obj
        elif kind == "events":
            events = _records(obj)
        elif kind in files:
            files[kind] = [dict(r) for r in _records(obj)]
        else:
            raise ValueError(f"未知档案类型: {kind}")
    store = _build_store(files, events)
    from .engine import MonitoringEngine
    ds = Dataset(store=store, rulebook=rulebook, periods=periods, events=events)
    ds.engine = MonitoringEngine(store, rulebook)
    return ds
