"""时间轴与统计期间工具。

系统中一切“只在某段时间成立”的事实（候选药归属、跨区许可、集团关系、
销售披露口径）都表示为带生效区间的记录：``[start, end)``，``end`` 为
``None`` 表示至今有效。合并收购或授权转手因此只会改变对应生效区间，
不会回写历史。

日期统一使用 ``YYYY-MM-DD`` 字符串，避免时区歧义；金额按期初/期末锚定，
按天在统计期间之间分摊。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

DATE_FMT = "%Y-%m-%d"
OPEN_END: Optional[str] = None


def d(value: str) -> date:
    """解析并校验 ISO 日期。"""
    return date.fromisoformat(value)


def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_year(year: int) -> int:
    return 366 if is_leap_year(year) else 365


@dataclass(frozen=True)
class Interval:
    """左闭右开的生效区间 ``[start, end)``。"""

    start: str
    end: Optional[str] = None

    def __post_init__(self):
        s = d(self.start)
        if self.end is not None and d(self.end) <= s:
            raise ValueError(f"无效区间: {self.start} ~ {self.end}")

    def contains(self, day: str) -> bool:
        if d(day) < d(self.start):
            return False
        return self.end is None or d(day) < d(self.end)

    def intersect(self, other: "Interval") -> Optional["Interval"]:
        lo = max(d(self.start), d(other.start))
        self_end = d(self.end) if self.end else None
        other_end = d(other.end) if other.end else None
        ends = [x for x in (self_end, other_end) if x is not None]
        hi = min(ends) if ends else None
        if hi is not None and lo >= hi:
            return None
        return Interval(lo.isoformat(), hi.isoformat() if hi else None)

    def day_count(self) -> int:
        """区间覆盖的天数；开口区间无法计数，抛错。"""
        if self.end is None:
            raise ValueError("开口区间没有有限天数")
        return (d(self.end) - d(self.start)).days

    def segments(self) -> Iterable[tuple[int, int]]:
        """按日历年切成 ``(year, days)`` 段（仅用于有限区间）。"""
        cur = d(self.start)
        end = d(self.end)
        while cur < end:
            year_end = date(cur.year + 1, 1, 1)
            stop = min(end, year_end)
            yield cur.year, (stop - cur).days
            cur = stop


@dataclass(frozen=True)
class StatsPeriod:
    """一个冻结/发布的统计期（这里按日历年建模）。"""

    code: str
    label: str
    start: str
    end: Optional[str]

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)

    def contains(self, day: str) -> bool:
        return self.interval.contains(day)


def annual_period(year: int) -> StatsPeriod:
    return StatsPeriod(
        code=str(year),
        label=f"{year} 年度",
        start=f"{year}-01-01",
        end=f"{year + 1}-01-01",
    )


def allocate(amount: float, span: Interval, period: StatsPeriod) -> float:
    """把覆盖 ``span`` 的金额按天比例分摊进 ``period``。

    用于财年与日历年不一致、或披露只覆盖年内一段区间的归一化。
    开口区间不得携带金额（金额披露必有期末日）。
    """
    overlap = span.intersect(period.interval)
    if overlap is None:
        return 0.0
    total = span.day_count()
    return amount * overlap.day_count() / total


def latest_effective(records: Iterable[tuple[Interval, object]], on_day: str) -> Optional[object]:
    """在若干 ``(区间, 值)`` 中返回 ``on_day`` 当天生效、且开始日最晚的值。"""
    candidates = [
        (iv.start, val)
        for iv, val in records
        if iv.contains(on_day)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]
