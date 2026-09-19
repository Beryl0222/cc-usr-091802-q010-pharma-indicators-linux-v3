"""医药规划指标证据库（pharma-indicator-evidence）。

以化合物、候选药、适应证、首次关键发现、各法域批准、许可关系和销售披露
分别建档；证据带来源链、版本与双时间（事件生效区间 + 系统记录区间）。
所有发布结论都可回溯到证据版本与换算规则版本，手填汇总数无法进入发布链。
"""

from .temporal import Date, Interval, FXRate
from .model import (
    Party,
    Source,
    Compound,
    Candidate,
    Indication,
    KeyDiscovery,
    Approval,
    Relation,
    SalesDisclosure,
    Opinion,
    Review,
    Restriction,
)
from .rules import RuleBook, RuleBookVersion
from .store import EvidenceStore, VersionedRecord, SecurityError, ValidationError
from .consolidation import SalesLine, ConsolidationResult, consolidate_sales
from .fic import (
    FICAssessment,
    FICStatus,
    assess_fic,
)
from .snapshot import Snapshot, freeze_snapshot, reproduce_snapshot
from .engine import MonitoringEngine, Determination
from .loader import load_dataset, load_dataset_dir

__all__ = [
    "Date",
    "Interval",
    "FXRate",
    "Party",
    "Source",
    "Compound",
    "Candidate",
    "Indication",
    "KeyDiscovery",
    "Approval",
    "Relation",
    "SalesDisclosure",
    "Opinion",
    "Review",
    "Restriction",
    "RuleBook",
    "RuleBookVersion",
    "EvidenceStore",
    "VersionedRecord",
    "SecurityError",
    "ValidationError",
    "SalesLine",
    "ConsolidationResult",
    "consolidate_sales",
    "FICAssessment",
    "FICStatus",
    "assess_fic",
    "Snapshot",
    "freeze_snapshot",
    "reproduce_snapshot",
    "MonitoringEngine",
    "Determination",
    "load_dataset",
    "load_dataset_dir",
]
