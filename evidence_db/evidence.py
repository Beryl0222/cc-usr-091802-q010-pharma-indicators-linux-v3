"""证据档案模型。

七类核心档案分别独立建档，彼此通过标识关联：

* :class:`Compound`            化合物（规范名 + 各国别名）
* :class:`Candidate`           候选药/品种（开发项目，归属随区间变化）
* :class:`Indication`          适应证
* :class:`FirstDiscovery`      首次关键发现（首创依据，可有多条竞争主张）
* :class:`Approval`            各法域批准
* :class:`LicenseRelation`     共同开发 / 跨区许可关系（带生效区间）
* :class:`SalesDisclosure`     销售披露（财年、币种、重述链）

另有 :class:`GroupRelation`（集团归属区间，用于内部交易抵销）、
:class:`AttributionClaim`（竞争性归属意见）与 :class:`EvidenceGap`（证据缺口）。

所有记录都是**只追加、不可变**的：更正不是修改旧记录，而是写入一条
``supersedes`` 指向旧记录的新版本。每条记录都携带来源与保密等级。
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from .timeline import Interval


# ---------------------------------------------------------------------------
# 来源与保密
# ---------------------------------------------------------------------------

class Confidentiality(str, enum.Enum):
    PUBLIC = "public"            # 公开年报、监管机构公告
    RESTRICTED = "restricted"    # 限监测机构内部
    CONFIDENTIAL = "confidential"  # 未公开合同，仅授权人员可见


@dataclass(frozen=True)
class Source:
    """一条证据的出处。"""

    doc_id: str                 # 文档标识（年报、公告、合同编号）
    title: str
    publisher: str              # 发布方
    published_on: str           # 发布/披露日期（决定适用的规则版本）
    locator: str = ""           # 页码、章节、链接
    confidentiality: Confidentiality = Confidentiality.PUBLIC
    contract_grant: Optional[str] = None  # confidential 时，所需授权标识

    def as_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out["confidentiality"] = self.confidentiality.value
        return out


# ---------------------------------------------------------------------------
# 记录基类与版本链
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    id: str
    version: int = 1
    supersedes: Optional[str] = None  # 被取代的旧版本 version_id
    recorded_at: str = "2026-01-01"   # 进入证据库的日期
    tags: tuple[str, ...] = ()

    @property
    def version_id(self) -> str:
        """同一稳定 ``id`` 下每个版本的唯一标识。"""
        return f"{self.id}#v{self.version}"

    def digest(self) -> str:
        """内容指纹（不含版本管理字段），用于冻结与血缘。"""
        payload = {
            "type": type(self).__name__,
            "id": self.id,
            "body": {
                k: v
                for k, v in dataclasses.asdict(self).items()
                if k not in ("version", "supersedes", "recorded_at", "tags")
            },
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        out = dataclasses.asdict(self)
        return _jsonify(out)


def _json_default(obj):
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, Interval):
        return dataclasses.asdict(obj)
    raise TypeError(f"不可序列化: {obj!r}")


def _jsonify(value):
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Interval):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# 七类核心档案
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Compound(Record):
    """化合物。跨国别名在此归一到同一 ``compound_id``。"""

    canonical_name: str = ""
    aliases: tuple[str, ...] = ()           # 各国商品名/研发代号/通用名
    moa_class: str = ""                     # 作用机制类别（用于“同类”判定）
    source: Optional[Source] = None
    note: str = ""


@dataclass(frozen=True)
class Indication(Record):
    """适应证。"""

    code: str = ""                          # 如 ICD/MeSH 或内部口径码
    label: str = ""
    source: Optional[Source] = None


@dataclass(frozen=True)
class Candidate(Record):
    """候选药/品种：一个开发中的药品项目，绑定唯一化合物。

    ``originators`` 为发起/共同开发企业；共同开发、收购导致的开发方变化
    通过区间化的 :class:`LicenseRelation` / :class:`GroupRelation` 表达，
    这里只记录发起事实。
    """

    compound_id: str = ""
    code: str = ""                          # 品种代码（规划口径）
    name: str = ""
    originators: tuple[str, ...] = ()       # 发起企业
    indications: tuple[str, ...] = ()
    first_initiated_on: Optional[str] = None  # 首次立项/进入临床时间
    source: Optional[Source] = None
    note: str = ""


@dataclass(frozen=True)
class FirstDiscovery(Record):
    """首次关键发现主张。谁先发现、何时、新颖性何在。

    同一品种存在多条相互竞争的主张时全部保留，交科学复核裁决。
    """

    candidate_id: str = ""
    discoverer: str = ""                    # 发现方（企业/机构）
    discovered_on: str = ""
    novelty: str = ""                       # 新靶点/新机制/新结构类型
    evidence_summary: str = ""
    source: Optional[Source] = None


@dataclass(frozen=True)
class Approval(Record):
    """某法域的监管批准。批准日期被更正时以新版本取代。"""

    candidate_id: str = ""
    indication_id: Optional[str] = None
    jurisdiction: str = ""                  # US/FDA, EU/EMA, CN/NMPA ...
    approval_type: str = ""                 # NCE/NME、新适应证…
    approved_on: str = ""
    first_in_class_designation: bool = False  # 监管是否明示同类首创
    source: Optional[Source] = None


@dataclass(frozen=True)
class LicenseRelation(Record):
    """共同开发 / 许可关系，按生效区间与地域划分。

    ``grantor`` 授权给 ``grantee``；共同开发时双方经济分成由
    ``grantee_share`` 表达。转手（新授权/收购）新增一条区间相接的记录，
    旧区间保持有效。
    """

    candidate_id: str = ""
    grantor: str = ""
    grantee: str = ""
    relation_kind: str = "license"          # license | codevelopment | assignment
    regions: tuple[str, ...] = ()           # 授权地域
    interval: Interval = field(default_factory=lambda: Interval("1900-01-01"))
    grantee_share: float = 1.0              # 受让方在该地域的经济权益比例
    source: Optional[Source] = None


@dataclass(frozen=True)
class GroupRelation(Record):
    """集团归属区间：``unit`` 在区间内是 ``group`` 的成员。

    内部交易抵销只抵销披露日处于同一集团的双方之间的流量。
    """

    unit: str = ""
    group: str = ""
    interval: Interval = field(default_factory=lambda: Interval("1900-01-01"))
    source: Optional[Source] = None


class RevenueKind(str, enum.Enum):
    """收入性质。只有面向终端市场的产品销售计入全球销售额。"""

    END_MARKET_NET_SALES = "end_market_net_sales"   # 终端市场净销售（计入）
    END_MARKET_GROSS_SALES = "end_market_gross_sales"
    ROYALTY_INCOME = "royalty_income"               # 特许权使用费（同一销售流，不重复加总）
    COLLABORATION_PROFIT_SHARE = "collab_profit_share"
    MILESTONE = "milestone"
    INTERCOMPANY_SALES = "intercompany_sales"       # 集团内部销售（抵销）


@dataclass(frozen=True)
class SalesDisclosure(Record):
    """一笔销售披露。

    * 金额覆盖 ``period``（企业财年或其中一段）；
    * ``disclosed_on`` 是年报/公告发布日，决定换算规则版本；
    * 重述时写入新版本记录并以 ``supersedes`` 指向原披露；
    * ``stream_key`` 标识其背后的终端销售流：同一 stream 的多笔披露
      （如许可方特许权费 + 被许可方产品销售）去重时只取一条主腿；
    * ``region`` 与许可地域一致，重叠会被引擎标为冲突而非重复计入。
    """

    candidate_id: str = ""
    reporting_company: str = ""
    counterparty: Optional[str] = None      # 关联交易对手（内部腿）
    indication_id: Optional[str] = None
    region: str = ""
    amount: float = 0.0
    currency: str = ""
    period: Interval = field(default_factory=lambda: Interval("2000-01-01", "2001-01-01"))
    fiscal_year_label: str = ""             # 企业披露的财年标签
    revenue_kind: RevenueKind = RevenueKind.END_MARKET_NET_SALES
    stream_key: str = ""
    disclosed_on: str = ""
    source: Optional[Source] = None
    note: str = ""


# ---------------------------------------------------------------------------
# 竞争性归属意见与证据缺口
# ---------------------------------------------------------------------------

class ClaimStatus(str, enum.Enum):
    OPEN = "open"                  # 待裁决
    PREFERRED = "preferred"        # 复核后采纳
    REJECTED = "rejected"          # 复核后驳回


@dataclass(frozen=True)
class AttributionClaim(Record):
    """来源冲突时的一条竞争性归属意见。

    例如同一化合物在 A 国叫 X、在 B 国叫 Y；两公司都主张首创；
    一笔收入双方都声称归己。意见可附在任何主题记录上（``subject_id``）。
    """

    subject_id: str = ""
    topic: str = ""               # identity | first_in_class | sales_attribution
    claimant: str = ""
    assertion: str = ""
    rationale: str = ""
    status: ClaimStatus = ClaimStatus.OPEN
    resolved_by_review: Optional[str] = None
    source: Optional[Source] = None


@dataclass(frozen=True)
class EvidenceGap(Record):
    """证据缺口：缺什么、阻塞哪个结论、是否致命。"""

    subject_id: str = ""
    missing: str = ""
    blocking: bool = True
    source: Optional[Source] = None
