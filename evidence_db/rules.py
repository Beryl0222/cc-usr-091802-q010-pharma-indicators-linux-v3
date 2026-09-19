"""有版本的换算规则：币种、财年、地域口径。

规则本身也只追加、按生效日版本化。一笔在 ``disclosed_on`` 发布的披露，
永远采用**当天有效**的规则版本换算；冻结结论里记录所用规则版本的标识与
指纹。即使后来发布了新汇率或新的地域口径，历史年度已发布数字仍可按旧
版本逐位复现。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .timeline import Interval, d


@dataclass(frozen=True)
class FxVersion:
    """一版汇率表。``rates[year][ccy]`` 为该年均汇：1 基准币 = rate 单位外币。

    即 ``基准币金额 = 外币金额 / rate``。用年平均汇率换算年度销售额，
    与年报披露惯例一致；跨年区间再由引擎按天加权。
    """

    version: str
    effective_from: str          # 该版本规则开始适用的披露日
    base_currency: str
    rates: dict[int, dict[str, float]] = field(default_factory=dict)
    note: str = ""

    def rate(self, currency: str, year: int) -> float:
        if currency == self.base_currency:
            return 1.0
        try:
            return self.rates[year][currency]
        except KeyError:
            # 退回最近的已知年份，保证缺口可见而不是崩溃；缺口由调用方登记
            years = sorted(y for y in self.rates if currency in self.rates[y])
            if not years:
                raise RuleGap(f"缺少汇率: {currency} (规则 {self.version})")
            return self.rates[years[-1]][currency]

    def to_base(self, amount: float, currency: str, year: int) -> float:
        return amount / self.rate(currency, year)


@dataclass(frozen=True)
class FiscalConvention:
    """企业财年口径。``year_end_mmdd`` 为财年结束日（如 03-31）。"""

    company: str
    year_end_mmdd: str = "12-31"

    def fiscal_interval(self, fy_label: str) -> Interval:
        """把财年标签（结束所在公历年）换算成实际区间。

        财年区间为「上年结日次日 ~ 本年结日」，右端点开到结日次日。
        例：结日 03-31、``fy_label=2025`` → 2024-04-01 ~ 2025-04-01（开）。
        结日 12-31 → 当年 01-01 ~ 次年 01-01（开）。
        跨越公历年的部分由引擎按天分摊回日历年统计期。
        """
        from datetime import timedelta

        fy = int(fy_label)
        mm, dd = (int(x) for x in self.year_end_mmdd.split("-"))
        year_end = date(fy, mm, dd)
        start = date(fy - 1, mm, dd) + timedelta(days=1)
        return Interval(start.isoformat(), _next_day(year_end))


def _next_day(day: date) -> str:
    from datetime import timedelta

    return (day + timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class GeoRule:
    """地域口径：披露地域 → 规范地域，以及全球口径说明。"""

    region_map: dict[str, str] = field(default_factory=dict)
    global_territories: tuple[str, ...] = ("WORLD",)
    note: str = ""

    def canonical(self, region: str) -> str:
        return self.region_map.get(region, region)


@dataclass(frozen=True)
class IndicatorRule:
    """指标口径（与 fixtures/sample.json 的 code 对齐）。"""

    code: str
    unit: str
    params: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleBookVersion:
    """一版完整换算规则。"""

    version: str
    effective_from: str
    fx: FxVersion
    fiscal: dict[str, FiscalConvention] = field(default_factory=dict)
    geo: GeoRule = field(default_factory=GeoRule)
    indicators: dict[str, IndicatorRule] = field(default_factory=dict)
    note: str = ""

    def digest(self) -> str:
        payload = {
            "version": self.version,
            "effective_from": self.effective_from,
            "base": self.fx.base_currency,
            "rates": self.fx.rates,
            "fiscal": {k: v.year_end_mmdd for k, v in self.fiscal.items()},
            "geo": self.geo.region_map,
            "indicators": {
                k: {"unit": v.unit, "params": v.params}
                for k, v in self.indicators.items()
            },
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def fiscal_interval(self, company: str, fy_label: str) -> Optional[Interval]:
        conv = self.fiscal.get(company)
        if conv is None:
            return None
        return conv.fiscal_interval(fy_label)


class RuleGap(Exception):
    """规则缺口（如缺汇率）。引擎将其登记为待确认证据而非静默放行。"""


class VersionedRuleBook:
    """按生效日管理换算规则版本，只追加。"""

    def __init__(self) -> None:
        self._versions: list[RuleBookVersion] = []

    def add(self, version: RuleBookVersion) -> None:
        self._versions.append(version)
        self._versions.sort(key=lambda v: d(v.effective_from))

    def versions(self) -> list[RuleBookVersion]:
        return list(self._versions)

    def effective_on(self, day: str) -> RuleBookVersion:
        """返回 ``day`` 当天有效的最新版本；当天无任何版本则报错。"""
        chosen = None
        for v in self._versions:
            if d(v.effective_from) <= d(day):
                chosen = v
            else:
                break
        if chosen is None:
            raise RuleGap(f"{day} 之前没有可用的换算规则版本")
        return chosen

    def get(self, version: str) -> RuleBookVersion:
        for v in self._versions:
            if v.version == version:
                return v
        raise KeyError(f"未知规则版本: {version}")


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------

BLOCKBUSTER_USD = 1_000_000_000.0


def default_indicator_rules() -> dict[str, IndicatorRule]:
    return {
        "fic-global-share": IndicatorRule(
            code="fic-global-share",
            unit="percent",
            params={
                # 分子：统计期内全球首创（FIC）冻结结论数；
                # 分母：统计期内取得任一法域首次批准的品种数。
                "scale": 100.0,
            },
        ),
        "companies-over-10b": IndicatorRule(
            code="companies-over-10b",
            unit="count",
            params={"blockbuster_usd": BLOCKBUSTER_USD},
        ),
    }
