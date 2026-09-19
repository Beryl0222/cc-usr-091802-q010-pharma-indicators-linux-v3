"""时间、生效区间与币种换算基础类型。

* ``Interval`` —— 业务事件的生效区间（含端点）。并购、授权转手只影响其覆盖
  的区间，区间之外不受影响；区间端点用 ISO ``YYYY-MM-DD`` 字符串表示，
  ``None`` 表示开放端。
* ``FXRate`` —— 某基准日生效的汇率，挂换算规则版本使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 开放端
OPEN: Optional[str] = None


def _cmp(a: Optional[str], b: Optional[str]) -> int:
    """ISO 日期字符串可直接按字典序比较；``None`` 代表无穷远。"""
    if a is None and b is None:
        return 0
    if a is None:
        return 1
    if b is None:
        return -1
    return (a > b) - (a < b)


def overlap_months(start: Optional[str], end: Optional[str]) -> int:
    """[start,end] 区间包含的月份数（按日历月计数，含首尾月，开放端返回 0）。

    仅用于展示与校验；真正的销售期间由披露自带的期间键决定。
    """
    if start is None or end is None:
        return 0
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    return (ey * 12 + em) - (sy * 12 + sm) + 1


@dataclass(frozen=True)
class Interval:
    """业务事件的生效区间（含端点）。"""

    start: Optional[str] = None  # ISO 日期；None = 起始开放
    end: Optional[str] = None  # ISO 日期；None = 结束开放（仍在生效）

    def __post_init__(self):
        if self.start is not None and self.end is not None:
            if _cmp(self.start, self.end) > 0:
                raise ValueError(f"生效区间起点晚于终点: {self.start} > {self.end}")

    @property
    def is_open_ended(self) -> bool:
        return self.end is None

    def contains(self, day: str) -> bool:
        return _cmp(self.start, day) <= 0 and (
            self.end is None or _cmp(day, self.end) <= 0
        )

    def overlaps(self, other: "Interval") -> bool:
        if self.start is not None and other.end is not None and _cmp(self.start, other.end) > 0:
            return False
        if other.start is not None and self.end is not None and _cmp(other.start, self.end) > 0:
            return False
        return True

    def intersection(self, other: "Interval") -> Optional["Interval"]:
        if not self.overlaps(other):
            return None
        start = self.start if other.start is None else (
            other.start if self.start is None else max(self.start, other.start)
        )
        end = self.end if other.end is None else (
            other.end if self.end is None else min(self.end, other.end)
        )
        return Interval(start, end)

    def as_filter(self) -> dict:
        return {"start": self.start, "end": self.end}

    def __str__(self) -> str:
        return f"[{self.start or '…'}, {self.end or '…'}]"


@dataclass(frozen=True)
class Date:
    """一个 ISO 日历日，便于在结论中显式标注关键日期。"""

    iso: str

    def __post_init__(self):
        if len(self.iso) != 10 or self.iso[4] != "-" or self.iso[7] != "-":
            raise ValueError(f"非法 ISO 日期: {self.iso!r}")
        int(self.iso[:4]); int(self.iso[5:7]); int(self.iso[8:10])

    def __str__(self) -> str:
        return self.iso


@dataclass(frozen=True)
class FXRate:
    """基准日 ``rate`` 单位外币折合多少本币（target_ccy）。"""

    base_date: str
    source_ccy: str
    target_ccy: str
    rate: float

    def __post_init__(self):
        if self.rate <= 0:
            raise ValueError("汇率必须为正数")
