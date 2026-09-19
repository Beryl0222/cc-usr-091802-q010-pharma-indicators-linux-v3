"""领域实体：七类证据分别建档，外加竞争性意见、复核与访问限制。

每类证据是独立的"档"（archive），通过 id 相互引用，而不是预先汇总：

化合物 Compound / 候选药 Candidate / 适应证 Indication /
首次关键发现 KeyDiscovery / 各法域批准 Approval /
许可关系 Relation / 销售披露 SalesDisclosure。

别名（含不同国家的别名）挂在化合物与候选药上；共同开发、跨区许可、
并购与授权转手统一表达为带生效区间的 :class:`Relation`。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

from .temporal import Date, Interval

# 关系（许可与权属）类型
REL_CODEVELOPMENT = "codevelopment"      # 共同开发
REL_LICENSE = "license"                  # 分地区许可（授权）
REL_ASSIGNMENT = "assignment"            # 授权/资产转手
REL_ACQUISITION = "acquisition"          # 合并收购（整体主体并入集团）

# 收入口径
REV_BOOKED = "booked"                    # 自有/确认的产品销售收入
REV_ROYALTY = "royalty"                  # 让渡权利后取得的特许权使用费
REV_PARTNER_SHARE = "partner-share"      # 归属合作伙伴、由披露方代收的份额
REV_INTERCOMPANY = "intercompany"        # 集团内部销售（合并时必须抵销）

# 证据来源类型
SRC_ANNUAL_REPORT = "annual-report"
SRC_REGULATOR = "regulator"
SRC_PRESS = "press-release"
SRC_CONTRACT = "contract"
SRC_INTERNAL = "internal"
SRC_TRADE = "trade-database"

# 意见主题 / 复核类型
SUBJECT_FIC = "fic-attribution"          # 首创归属之争
SUBJECT_SALES = "sales-attribution"      # 销售归属/口径之争
REVIEW_SCIENCE = "science"               # 科学复核
REVIEW_STATS = "stats"                   # 统计复核

# 访问密级（数字越大要求越高）
CLEARANCE_PUBLIC = 0
CLEARANCE_RESTRICTED = 1
CLEARANCE_CONFIDENTIAL = 2


def _flatten(obj: Any) -> Any:
    if isinstance(obj, Interval):
        return {"start": obj.start, "end": obj.end}
    if isinstance(obj, Date):
        return obj.iso
    if isinstance(obj, dict):
        return {k: _flatten(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_flatten(v) for v in obj]
    return obj


@dataclass
class Entity:
    """所有证据实体的基类：稳定 id + 来源链。"""

    id: str
    source_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {f.name: _flatten(getattr(self, f.name)) for f in fields(self)}
        out["kind"] = KIND[type(self)]
        return out


@dataclass
class Party(Entity):
    """公司/集团主体。同一 ``group_id`` 内互为关联方（用于内部交易抵销）。"""

    name: str = ""
    aliases: List[str] = field(default_factory=list)
    group_id: Optional[str] = None
    party_type: str = "company"          # company / jv
    country: Optional[str] = None


@dataclass
class Source(Entity):
    """证据出处：年报、监管机构、新闻稿、合同、内部材料等。"""

    title: str = ""
    publisher: str = ""
    doc_type: str = SRC_PRESS
    published_on: Optional[str] = None
    fiscal_year_label: Optional[str] = None
    url: Optional[str] = None
    confidential: bool = False
    clearance_required: int = CLEARANCE_PUBLIC


@dataclass
class Compound(Entity):
    """化合物本体；不同国家的别名统一收在 aliases。"""

    name: str = ""
    aliases: List[str] = field(default_factory=list)
    cas: Optional[str] = None
    smiles: Optional[str] = None
    molecular_target: Optional[str] = None
    modality: Optional[str] = None


@dataclass
class Candidate(Entity):
    """候选药/药品（同一化合物在不同公司、不同法域可能有不同代号与商品名）。"""

    compound_id: Optional[str] = None
    code: Optional[str] = None
    names_by_jurisdiction: Dict[str, str] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    originator_party_ids: List[str] = field(default_factory=list)
    # 首创地位的初步定性，最终以冻结结论为准
    novelty_class: Optional[str] = None    # first-in-class / best-in-class / me-too


@dataclass
class Indication(Entity):
    """适应证（一个候选药可对应多个适应证）。"""

    candidate_id: Optional[str] = None
    term: str = ""
    icd: Optional[str] = None
    oncology_flag: bool = False


@dataclass
class KeyDiscovery(Entity):
    """首次关键发现：首创认定的科学依据，按候选药/化合物归档。"""

    candidate_id: Optional[str] = None
    compound_id: Optional[str] = None
    title: str = ""
    discovered_on: Optional[str] = None
    party_id: Optional[str] = None
    priority_weight: float = 1.0          # 多发现冲突时的权重
    novelty_claim: Optional[str] = None   # 新靶点/新机制/新结构类型


@dataclass
class Approval(Entity):
    """各法域监管批准。批准日期被更正时以新版本记录，不覆盖历史。"""

    candidate_id: Optional[str] = None
    indication_id: Optional[str] = None
    jurisdiction: str = ""
    agency: str = ""
    decision_date: Optional[str] = None
    action: str = "approval"              # approval / withdrawal / correction
    first_in_jurisdiction_flag: bool = False


@dataclass
class Relation(Entity):
    """共同开发 / 跨区许可 / 授权转手 / 并购，统一带生效区间与地域。

    并购（acquisition）使标的方自生效日起并入收购方集团；许可（license）
    描述哪些地域的商业化权利让渡给哪一方，以及让渡后权利金如何记账。
    区间外的销售不受该关系影响。
    """

    candidate_id: Optional[str] = None
    relation_type: str = REL_LICENSE
    grantor_party_id: Optional[str] = None   # 许可方/出让方
    grantee_party_id: Optional[str] = None   # 被许可方/受让方
    regions: List[str] = field(default_factory=list)  # 空 = 全球
    valid: Interval = field(default_factory=Interval)
    revenue_kind_for_grantee: str = REV_BOOKED
    revenue_kind_for_grantor: str = REV_ROYALTY
    title: Optional[str] = None
    supersedes_relation_id: Optional[str] = None
    confidential: bool = False


@dataclass
class SalesDisclosure(Entity):
    """一条销售披露（按公司×地域×财年期间）。

    ``restatement_of`` 指向被其重述的旧披露；企业重述年报即新增新版本/新记录，
    旧记录保留，已发布数字仍可复现。
    """

    candidate_id: Optional[str] = None
    party_id: Optional[str] = None
    region: str = ""
    period_key: str = ""                    # 归一后的期间键，如 FY2025 / 2025Q1
    fiscal_year_label: str = ""             # 披露方原始财年标签
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    currency: str = ""
    amount: float = 0.0
    revenue_kind: str = REV_BOOKED
    includes_partner_share: bool = False    # 是否包含代收的合作方份额
    partner_party_id: Optional[str] = None
    partner_share_amount: float = 0.0       # 其中代收、归属合作方的金额（原币种）
    counterparty_party_id: Optional[str] = None  # 内部销售的对手方（用于抵销）
    restatement_of: Optional[str] = None
    note: Optional[str] = None


@dataclass
class Opinion(Entity):
    """竞争性归属意见。来源冲突时不强行合并，并列登记，等待复核择一采纳。"""

    subject: str = SUBJECT_FIC
    candidate_id: Optional[str] = None
    proponent: str = ""                     # 提出方/分析师
    claim: str = ""
    proposed: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    confidence: str = "medium"              # low/medium/high


@dataclass
class Review(Entity):
    """复核记录：科学复核与统计复核两类，冻结首创结论前必须各有通过记录。"""

    candidate_id: Optional[str] = None
    review_type: str = REVIEW_SCIENCE
    reviewer: str = ""
    decision: str = "approved"              # approved / requested-changes / rejected
    opinion_id: Optional[str] = None
    rulebook_version: Optional[str] = None
    comment: Optional[str] = None


@dataclass
class Restriction(Entity):
    """未公开合同/内部证据的访问控制：按记录 id 限定最低密级与授权范围。"""

    record_id: str = ""
    record_kind: str = ""
    minimum_clearance: int = CLEARANCE_CONFIDENTIAL
    allowed_actors: List[str] = field(default_factory=list)  # 空 = 仅按密级
    reason: str = ""


KIND: Dict[type, str] = {}
_FORWARD: Dict[str, type] = {}
_ALL = [
    Party, Source, Compound, Candidate, Indication, KeyDiscovery,
    Approval, Relation, SalesDisclosure, Opinion, Review, Restriction,
]
for _cls in _ALL:
    _tag = _cls.__name__.lower()
    KIND[_cls] = _tag
    _FORWARD[_tag] = _cls

# interval / date 字段集合（用于通用反序列化）
_INTERVAL_FIELDS = {"valid"}
_DATE_FIELDS: set = set()


def inflate(data: Dict[str, Any]) -> Entity:
    """根据 ``kind`` 把普通 dict 还原成对应实体。"""
    kind = data.get("kind")
    if kind not in _FORWARD:
        raise ValueError(f"未知实体类型: {kind!r}")
    cls = _FORWARD[kind]
    valid_names = {f.name for f in fields(cls)}
    kwargs: Dict[str, Any] = {}
    for key, value in data.items():
        if key == "kind" or key not in valid_names:
            continue
        if key in _INTERVAL_FIELDS and isinstance(value, dict):
            value = Interval(value.get("start"), value.get("end"))
        kwargs[key] = value
    return cls(**kwargs)  # type: ignore[arg-type]
