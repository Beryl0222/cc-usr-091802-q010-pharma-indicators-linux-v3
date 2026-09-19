"""有版本的换算规则：币种、财年、地域口径归一。

规则本身也纳入版本管理：每条 :class:`RuleBookVersion` 带有生效记录时间，
快照冻结时钉住所用规则版本，历史发布用旧版本即可逐字复现，不会被后来
更新的汇率或口径改变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .temporal import FXRate

ANCHOR_CCY = "CNY"


class RuleGapError(Exception):
    """缺少可用换算规则（汇率/财年/地域映射），按证据缺口处理，不得臆造。"""


@dataclass
class FiscalPeriod:
    """把披露方原始财年标签归一到统计期间。"""

    period_key: str
    start: str
    end: str


@dataclass
class RuleBookVersion:
    """某一版换算规则。"""

    version: str
    recorded_at: str                      # 该版本进入系统的记录时间
    anchor_ccy: str = ANCHOR_CCY
    # 汇率：外币 -> 锚币种，按基准日排列
    fx_rates: List[FXRate] = field(default_factory=list)
    # 财年口径：party_id -> {原始财年标签: 归一期间}
    fiscal_calendars: Dict[str, Dict[str, FiscalPeriod]] = field(default_factory=dict)
    # 地域口径：规范地域 -> 别名集合
    region_aliases: Dict[str, List[str]] = field(default_factory=dict)
    note: Optional[str] = None

    def __post_init__(self):
        self._fx: Dict[str, List[FXRate]] = {}
        for rate in self.fx_rates:
            if rate.target_ccy != self.anchor_ccy:
                raise ValueError(
                    f"汇率请统一挂到锚币种 {self.anchor_ccy}：{rate}"
                )
            self._fx.setdefault(rate.source_ccy, []).append(rate)
        for seq in self._fx.values():
            seq.sort(key=lambda r: r.base_date)
        self._alias_index: Dict[str, str] = {}
        for canonical, aliases in self.region_aliases.items():
            self._alias_index[canonical.upper()] = canonical
            for alias in aliases:
                self._alias_index[alias.upper()] = canonical

    # ---- 币种 ----
    def _rate_to_anchor(self, ccy: str, on_date: str) -> FXRate:
        if ccy == self.anchor_ccy:
            return FXRate(on_date, ccy, ccy, 1.0)
        seq = self._fx.get(ccy)
        if not seq:
            raise RuleGapError(f"规则版本 {self.version} 缺少 {ccy} 汇率")
        chosen: Optional[FXRate] = None
        for rate in seq:  # 已按基准日升序
            if rate.base_date <= on_date:
                chosen = rate
            else:
                break
        if chosen is None:
            raise RuleGapError(
                f"规则版本 {self.version} 中 {ccy} 在 {on_date} 或之前无汇率"
            )
        return chosen

    def convert(
        self, amount: float, source_ccy: str, on_date: str
    ) -> Tuple[float, Dict[str, Any]]:
        """把 ``amount`` 折算为锚币种，返回(金额, 换算说明)。"""
        if source_ccy == self.anchor_ccy:
            return float(amount), {
                "from": source_ccy,
                "to": self.anchor_ccy,
                "rate": 1.0,
                "rate_base_date": on_date,
                "rulebook_version": self.version,
            }
        rate = self._rate_to_anchor(source_ccy, on_date)
        converted = amount * rate.rate
        return converted, {
            "from": source_ccy,
            "to": self.anchor_ccy,
            "rate": rate.rate,
            "rate_base_date": rate.base_date,
            "rulebook_version": self.version,
        }

    # ---- 财年 ----
    def map_fiscal(
        self,
        party_id: str,
        fiscal_year_label: str,
        period_start: Optional[str],
        period_end: Optional[str],
    ) -> FiscalPeriod:
        """把原始财年标签归一为统计期间；无显历表时按期末日公历年兜底。"""
        cal = self.fiscal_calendars.get(party_id)
        if cal and fiscal_year_label in cal:
            return cal[fiscal_year_label]
        if period_end:
            year = period_end[:4]
            return FiscalPeriod(f"FY{year}", f"{year}-01-01", f"{year}-12-31")
        raise RuleGapError(
            f"无法归一财年：party={party_id} label={fiscal_year_label!r}，"
            "既无历表也无期末日"
        )

    # ---- 地域 ----
    def normalize_region(self, raw: str) -> str:
        key = (raw or "").strip().upper()
        if not key:
            raise RuleGapError("地域为空，无法归一")
        if key in self._alias_index:
            return self._alias_index[key]
        # 未配置映射时原样保留（大写规范），不臆造归属
        return raw.strip()


class RuleBook:
    """保存所有规则版本，按记录时间取最新或按版本号精确复现。"""

    def __init__(self) -> None:
        self._versions: Dict[str, RuleBookVersion] = {}

    def add_version(self, version: RuleBookVersion) -> None:
        if version.version in self._versions:
            raise ValueError(f"规则版本已存在: {version.version}")
        self._versions[version.version] = version

    def get(self, version: str) -> RuleBookVersion:
        if version not in self._versions:
            raise RuleGapError(f"规则版本不存在: {version}")
        return self._versions[version]

    def latest_as_of(self, recorded_at: str) -> RuleBookVersion:
        eligible = [v for v in self._versions.values() if v.recorded_at <= recorded_at]
        if not eligible:
            raise RuleGapError(f"{recorded_at} 之前没有可用规则版本")
        return max(eligible, key=lambda v: (v.recorded_at, v.version))

    @property
    def versions(self) -> List[str]:
        return sorted(self._versions)
