"""Documented Chinese compensation grammar: parsing with raw-text provenance.

Grammar (NFKC-normalized; only digit-flanking commas are treated as thousands
separators), clauses split on ``, ， ; ； 、 。``:

1. 月薪区间: ``20-25K`` / ``1.5-2.5万`` / ``40K``, optionally followed by
   ``12-24薪`` (extra months). Annual base = monthly * months.
2. 固定奖金区间: a range/single value containing ``固定奖金`` (or ``固薪奖``).
3. 目标奖金区间: ``目标奖金``/``绩效奖金``/bare ``奖金`` — bare 奖金 defaults
   to target, i.e. it is never guaranteed income.
4. 年包总额约束: ``年包35-40万（含上述各项）``/``含奖金``/``含提成`` — an
   annual total that *includes* every other component; the feasibility layer
   intersects it with the component sum instead of adding it.
5. 保底: ``保底37-38万/年`` — an annual guaranteed-total constraint.
6. 提成上不封顶: ``上不封顶``/``不封顶`` keeps an infinite upper bound
   (``high=None``); no arbitrary large integer is substituted.
7. Currency prefixes ``CNY/RMB/USD/EUR/HKD`` (default CNY). Plans in different
   currencies are parsed faithfully but never compared without a rate table.

Every parsed number keeps the exact :class:`~fractions.Fraction` value and the
source span (indices into the untouched raw input) via a normalization map.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

_UNIT_SCALE = {"": 1, "k": 1000, "w": 10000, "千": 1000, "万": 10000, "元": 1}
_CURRENCIES = ("CNY", "RMB", "USD", "EUR", "HKD")
_MONTH_MIN, _MONTH_MAX = 12, 24

_AMOUNT = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*(?P<low_unit>[kKwW千万元])?"
    r"(?:\s*(?:-|~|至|到)\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*(?P<high_unit>[kKwW千万元])?)?"
)
_MONTHS = re.compile(r"(?P<months>\d{1,2})\s*薪")
_OPEN_END = re.compile(r"上不封顶|不封顶")


class CompensationError(ValueError):
    """Raised when text cannot be parsed as a compensation plan."""


@dataclass(frozen=True)
class Interval:
    """Closed rational interval; ``high is None`` means +infinity."""

    low: Fraction
    high: Fraction | None

    def __post_init__(self) -> None:
        if self.high is not None and self.high < self.low:
            raise CompensationError("inverted interval")

    @property
    def bounded(self) -> bool:
        return self.high is not None

    def intersect(self, other: "Interval") -> "Interval | None":
        low = max(self.low, other.low)
        high = self.high if other.high is None else (
            other.high if self.high is None else min(self.high, other.high)
        )
        if high is not None and low > high:
            return None
        return Interval(low, high)

    def add(self, other: "Interval") -> "Interval":
        high = None if self.high is None or other.high is None else self.high + other.high
        return Interval(self.low + other.low, high)


def point(value: Fraction) -> Interval:
    return Interval(value, value)


@dataclass(frozen=True)
class NumberToken:
    """One parsed number with its exact value and raw-text source span."""

    value: Fraction
    raw: str
    start: int
    end: int


@dataclass(frozen=True)
class Clause:
    kind: str  # monthly|months|fixed_bonus|target_bonus|package|guarantee|commission
    text: str
    start: int
    end: int
    interval: Interval | None = None          # annualized money interval
    monthly_interval: Interval | None = None  # monthly units only
    months: int | None = None
    currency: str = "CNY"
    open_ended: bool = False
    numbers: tuple[NumberToken, ...] = ()


@dataclass(frozen=True)
class CompensationPlan:
    raw: str
    currency: str
    clauses: tuple[Clause, ...]
    parse_errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return bool(self.clauses) and not self.parse_errors

    def clauses_of(self, kind: str) -> tuple[Clause, ...]:
        return tuple(c for c in self.clauses if c.kind == kind)


def _normalize(raw: str) -> tuple[str, list[int]]:
    text = unicodedata.normalize("NFKC", raw)
    for dash in ("—", "–", "～", "〜"):
        text = text.replace(dash, "-")
    text = text.replace("至", "-").replace("到", "-")
    # Commas between digits are thousands separators ("10,000"); other commas
    # remain clause separators and must not be deleted.
    text = re.sub(r"(?<=\d)[,，](?=\d)", "", text)
    compact, index_map = "", []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        compact += ch
        index_map.append(i)
    return compact.strip("：:;；。"), index_map


def _raw_span(index_map: list[int], start: int, end: int) -> tuple[int, int]:
    if start >= len(index_map):
        return index_map[-1] + 1, index_map[-1] + 1
    return index_map[start], index_map[min(end, len(index_map)) - 1] + 1


def _fraction(num_text: str, unit: str | None) -> Fraction:
    return Fraction(num_text) * _UNIT_SCALE.get((unit or "").lower(), 1)


def _split_segments(text: str) -> list[tuple[int, int, str]]:
    # NFKC folds fullwidth punctuation onto ASCII, so list both forms.
    segments, start = [], 0
    for match in re.finditer(r"[,，;；、。]", text):
        if match.start() > start:
            segments.append((start, match.start(), text[start:match.start()]))
        start = match.end()
    if start < len(text):
        segments.append((start, len(text), text[start:]))
    return segments


def _parse_numbers(segment: str, base: int, index_map: list[int]) -> tuple[NumberToken, ...]:
    tokens = []
    for match in _AMOUNT.finditer(segment):
        if match.group("low") is None:
            continue
        low_unit = match.group("low_unit") or match.group("high_unit") or ""
        s, e = base + match.start("low"), base + match.end("low")
        tokens.append(NumberToken(
            _fraction(match.group("low"), low_unit),
            segment[match.start("low"):match.end("low")], *_raw_span(index_map, s, e),
        ))
        if match.group("high") is not None:
            high_unit = match.group("high_unit") or match.group("low_unit") or ""
            s = base + match.start("high")
            e = base + match.end("high")
            tokens.append(NumberToken(
                _fraction(match.group("high"), high_unit),
                segment[match.start("high"):match.end("high")],
                *_raw_span(index_map, s, e),
            ))
    return tuple(tokens)


def _money_interval(segment: str) -> Interval | None:
    match = _AMOUNT.search(segment)
    if not match or match.group("low") is None:
        return None
    low_unit = match.group("low_unit") or match.group("high_unit") or ""
    high_unit = match.group("high_unit") or match.group("low_unit") or ""
    low = _fraction(match.group("low"), low_unit)
    if match.group("high") is None:
        return point(low)
    return Interval(low, _fraction(match.group("high"), high_unit))


def _detect_currency(text: str) -> str:
    for code in _CURRENCIES:
        if code in text.upper():
            # RMB is the same currency as CNY for comparison purposes.
            return "CNY" if code == "RMB" else code
    return "CNY"


def _kind_of(segment: str) -> str | None:
    if "提成" in segment or "佣金" in segment:
        return "commission"
    # Package/guarantee win before generic 奖金: 年包…（含奖金）is a total.
    if "年包" in segment or ("总包" in segment and "保底" not in segment):
        return "package"
    if "保底" in segment or "保障" in segment:
        return "guarantee"
    if "固定奖金" in segment or "固薪奖" in segment:
        return "fixed_bonus"
    if "目标奖金" in segment or "绩效奖金" in segment or "奖金" in segment:
        return "target_bonus"
    if _AMOUNT.search(segment) and (
        _MONTHS.search(segment)
        or re.search(r"[kKwW千万]|月薪|/月|元/月", segment)
    ):
        return "monthly"
    return None


def parse_compensation(raw: str | None) -> CompensationPlan:
    """Parse the documented grammar, keeping raw text and per-number spans."""

    text = (raw or "").strip()
    if not text:
        raise CompensationError("empty compensation text")
    normalized, index_map = _normalize(text)
    currency = _detect_currency(normalized)
    clauses: list[Clause] = []
    errors: list[str] = []
    for base, end, segment in _split_segments(normalized):
        kind = _kind_of(segment)
        rstart, rend = _raw_span(index_map, base, end)
        if kind is None:
            errors.append(f"无法识别的薪酬片段: {text[rstart:rend]}")
            continue
        months_match = _MONTHS.search(segment)
        months: int | None = None
        if months_match:
            parsed = int(months_match.group("months"))
            if _MONTH_MIN <= parsed <= _MONTH_MAX:
                months = parsed
            else:
                errors.append(f"薪数超出 {_MONTH_MIN}-{_MONTH_MAX}: {text[rstart:rend]}")
        if kind == "monthly":
            monthly = _money_interval(segment)
            if monthly is None:
                errors.append(f"缺少月薪金额: {text[rstart:rend]}")
                continue
            factor = Fraction(months or 12)
            annual = Interval(
                monthly.low * factor,
                None if monthly.high is None else monthly.high * factor,
            )
            clauses.append(Clause(
                kind, text[rstart:rend], rstart, rend, annual, monthly,
                months, currency, False, _parse_numbers(segment, base, index_map),
            ))
            continue
        interval = _money_interval(segment)
        open_ended = bool(_OPEN_END.search(segment))
        if interval is None and not open_ended:
            errors.append(f"缺少金额: {text[rstart:rend]}")
            continue
        if open_ended:
            low = interval.low if interval is not None else Fraction(0)
            interval = Interval(low, None)
        clauses.append(Clause(
            kind, text[rstart:rend], rstart, rend, interval, None, months,
            currency, open_ended, _parse_numbers(segment, base, index_map),
        ))
    return CompensationPlan(raw=raw, currency=currency,
                            clauses=tuple(clauses), parse_errors=tuple(errors))
