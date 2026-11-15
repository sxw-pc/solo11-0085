"""Chinese salary expression parsing and monthly normalization."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_RANGE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*(?P<low_unit>[kKwW千万元])?\s*"
    r"(?:-|~|至|到)\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*(?P<high_unit>[kKwW千万元])?"
)
_SINGLE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[kKwW千万元])?")
_MONTHS = re.compile(r"(?P<months>\d{1,2})\s*薪")
_WORK_DAYS, _WORK_HOURS = 21.75, 8
_MULTIPLIERS = {"": 1, "k": 1_000, "w": 10_000, "千": 1_000, "万": 10_000, "元": 1}


@dataclass(frozen=True)
class SalaryInfo:
    raw: str = ""
    min_monthly: int | None = None
    max_monthly: int | None = None
    currency: str = "CNY"
    source_period: str = "month"
    months_per_year: int = 12
    negotiable: bool = False
    valid: bool = True
    empty: bool = False
    normalized: str = ""

    @property
    def annual_max(self) -> int | None:
        return None if self.max_monthly is None else self.max_monthly * self.months_per_year


def _clean(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).replace(",", "").replace("，", "")
    for dash in ("—", "–", "～", "〜"):
        text = text.replace(dash, "-")
    return re.sub(r"\s+", "", text.replace("至", "-").replace("到", "-")).strip("：:;；")


def _period(text: str) -> str:
    if any(token in text for token in ("时薪", "/时", "/小时", "元/时")):
        return "hour"
    if any(token in text for token in ("日薪", "/天", "/日", "元/天", "元/日")):
        return "day"
    if any(token in text for token in ("年薪", "/年", "万/年", "元/年")):
        return "year"
    return "month"


def _to_monthly(amount: float, unit: str, period: str) -> int:
    value = amount * _MULTIPLIERS.get((unit or "").lower(), 1)
    if period == "year":
        value /= 12
    elif period == "day":
        value *= _WORK_DAYS
    elif period == "hour":
        value *= _WORK_DAYS * _WORK_HOURS
    return int(value + 0.5)


def _normalized(low: int | None, high: int | None, months: int) -> str:
    if low is None and high is None:
        return "薪资未知"
    if low is not None and high is not None:
        body = f"{low}元/月" if low == high else f"{low}-{high}元/月"
    elif low is not None:
        body = f"{low}元/月起"
    else:
        body = f"{high}元/月以内"
    return body + (f"·{months}薪" if months != 12 else "")


def parse_salary(value: str | None) -> SalaryInfo:
    raw = (value or "").strip()
    if not raw:
        return SalaryInfo(raw="", empty=True, normalized="")
    text = _clean(raw)
    if not text:
        return SalaryInfo(raw=raw, empty=True, normalized="")
    if any(token in text for token in ("面议", "议薪", "保密", "待定")):
        return SalaryInfo(raw=raw, negotiable=True, normalized="面议")
    months = 12
    months_match = _MONTHS.search(text)
    if months_match:
        parsed_months = int(months_match.group("months"))
        if 12 <= parsed_months <= 24:
            months = parsed_months
        text = _MONTHS.sub("", text)
    period = _period(text)
    match = _RANGE.search(text)
    if match:
        low_unit = match.group("low_unit") or match.group("high_unit") or ""
        high_unit = match.group("high_unit") or low_unit
        try:
            low_amount = float(match.group("low"))
            high_amount = float(match.group("high"))
        except ValueError:
            return SalaryInfo(raw=raw, valid=False, normalized=raw)
        low = _to_monthly(low_amount, low_unit, period)
        high = _to_monthly(high_amount, high_unit, period)
    else:
        single = _SINGLE.search(text)
        if not single:
            return SalaryInfo(raw=raw, valid=False, normalized=raw)
        converted = _to_monthly(float(single.group("num")), single.group("unit") or "", period)
        low = high = converted
        if any(token in text for token in ("以上", "起")):
            high = None
        elif any(token in text for token in ("以下", "以内")):
            low = None
    if low is not None and high is not None and low > high:
        return SalaryInfo(raw=raw, valid=False, normalized=raw)
    if (low is not None and low <= 0) or (high is not None and high <= 0):
        return SalaryInfo(raw=raw, valid=False, normalized=raw)
    return SalaryInfo(
        raw=raw, min_monthly=low, max_monthly=high, source_period=period,
        months_per_year=months, normalized=_normalized(low, high, months),
    )


def salary_payload(info: SalaryInfo) -> dict[str, object]:
    return {
        "salary_min": info.min_monthly, "salary_max": info.max_monthly,
        "salary_text": info.normalized, "salary_months": info.months_per_year,
        "salary_period": info.source_period, "salary_negotiable": info.negotiable,
    }
