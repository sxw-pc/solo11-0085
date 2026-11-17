"""Exact rational feasibility over parsed compensation constraints.

The parser lives in :mod:`jobtrace.comp_parser`; this module intersects the
constraints into a feasible *annual* income set using bounded
:class:`fractions.Fraction` arithmetic — never float rounding.

Rules enforced here:

* 基本工资 (monthly × months) and 固定奖金 are guaranteed (strict) components.
* 目标/绩效奖金 and 提成 are variable targets, never guaranteed; their upper
  bound may be infinite (``上不封顶``) and stays infinite.
* A 年包总额 marked “含上述各项/含奖金/含提成” is intersected with the sum of
  components, never added to them (so A is 35-40万, not 63-81万).
* A standalone 保底 interval is itself the feasible set; alongside components
  it acts as a lower bound.
* When the intersection is empty, the two conflicting constraints and their
  exact intervals are reported instead of fabricating a ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .comp_parser import (
    Clause, CompensationError, CompensationPlan, Interval, NumberToken,
    parse_compensation, point,
)

__all__ = [
    "Clause", "CompensationError", "CompensationPlan", "Component", "Conflict",
    "Feasibility", "Interval", "NumberToken", "analyze_feasibility",
    "format_interval", "format_number", "parse_compensation", "point",
]

_LABELS = {
    "monthly": "基本工资（月薪×月数）", "fixed_bonus": "固定奖金",
    "target_bonus": "目标奖金（非保证）", "commission": "提成（非保证）",
    "package": "年包总额（含上述各项）", "guarantee": "保底年度总额",
}
_GUARANTEED = {"monthly", "fixed_bonus", "guarantee"}


@dataclass(frozen=True)
class Component:
    """One strict or target component of the feasibility explanation."""

    kind: str
    label: str
    guaranteed: bool
    interval: Interval
    sources: tuple[Clause, ...]


@dataclass(frozen=True)
class Conflict:
    """The two constraints whose intersection is empty."""

    left: str
    right: str
    left_interval: Interval
    right_interval: Interval
    detail: str


@dataclass(frozen=True)
class Feasibility:
    plan: CompensationPlan
    feasible: bool
    feasible_interval: Interval | None
    strict_interval: Interval
    target_interval: Interval
    package_interval: Interval | None
    components: tuple[Component, ...]
    conflicts: tuple[Conflict, ...]


def _merge(clauses: tuple[Clause, ...]) -> Interval | None:
    if not clauses:
        return None
    merged = clauses[0].interval
    for clause in clauses[1:]:
        merged = merged.intersect(clause.interval)
        if merged is None:
            return None
    return merged


def _sum_all(parts: list[Interval]) -> Interval:
    total = parts[0]
    for part in parts[1:]:
        total = total.add(part)
    return total


def _component(kind: str, interval: Interval, clauses: tuple[Clause, ...]) -> Component:
    return Component(kind, _LABELS[kind], kind in _GUARANTEED, interval, clauses)


def _same_kind_conflict(kind: str, clauses: tuple[Clause, ...]) -> Conflict:
    return Conflict(
        kind, kind, clauses[0].interval, clauses[-1].interval,
        f"同类 {_LABELS[kind]} 约束彼此无交集",
    )


def analyze_feasibility(plan: CompensationPlan) -> Feasibility:
    """Intersect every constraint; never add the inclusive package total."""

    components: list[Component] = []
    strict_parts: list[Interval] = []
    target_parts: list[Interval] = [point(Fraction(0))]
    conflicts: list[Conflict] = []

    for kind in ("monthly", "fixed_bonus"):
        clauses = plan.clauses_of(kind)
        merged = _merge(clauses)
        if clauses and merged is None:
            conflicts.append(_same_kind_conflict(kind, clauses))
        elif merged is not None:
            components.append(_component(kind, merged, clauses))
            strict_parts.append(merged)
    for kind in ("target_bonus", "commission"):
        clauses = plan.clauses_of(kind)
        merged = _merge(clauses)
        if clauses and merged is None:
            conflicts.append(_same_kind_conflict(kind, clauses))
        elif merged is not None:
            components.append(_component(kind, merged, clauses))
            target_parts.append(merged)

    strict = _sum_all(strict_parts) if strict_parts else point(Fraction(0))
    target = _sum_all(target_parts)
    package_clauses = plan.clauses_of("package")
    guarantee_clauses = plan.clauses_of("guarantee")
    package = _merge(package_clauses) if package_clauses else None
    guarantee = _merge(guarantee_clauses) if guarantee_clauses else None
    if package is not None:
        components.append(_component("package", package, package_clauses))
    if guarantee is not None:
        components.append(_component("guarantee", guarantee, guarantee_clauses))

    component_kinds = {c.kind for c in components} - {"guarantee", "package"}
    if guarantee is not None and not component_kinds:
        # Standalone guarantee: the offer itself defines the feasible set.
        feasible: Interval | None = guarantee
    else:
        feasible = strict.add(target)
        if guarantee is not None:
            narrowed = feasible.intersect(Interval(guarantee.low, None))
            if narrowed is None:
                conflicts.append(Conflict(
                    "component_sum", "guarantee", feasible, guarantee,
                    "各组成部分可行收入之和达不到保底年度总额",
                ))
                feasible = None
            else:
                feasible = narrowed
        if package is not None and feasible is not None:
            narrowed = feasible.intersect(package)
            if narrowed is None:
                conflicts.append(Conflict(
                    "component_sum", "package", feasible, package,
                    "各组成部分可行收入之和与含上述各项的年包总额无交集",
                ))
                feasible = None
            else:
                feasible = narrowed
    return Feasibility(
        plan=plan, feasible=not conflicts, feasible_interval=feasible,
        strict_interval=strict, target_interval=target,
        package_interval=package, components=tuple(components),
        conflicts=tuple(conflicts),
    )


def format_number(value: Fraction) -> str:
    """Exact finite decimal when possible, otherwise an explicit fraction."""

    if value.denominator == 1:
        return str(value.numerator)
    decimal = _exact_decimal(value)
    return decimal if decimal is not None else f"{value.numerator}/{value.denominator}"


def _exact_decimal(value: Fraction) -> str | None:
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator, twos = denominator // 2, twos + 1
    while denominator % 5 == 0:
        denominator, fives = denominator // 5, fives + 1
    if denominator != 1:
        return None
    places = max(twos, fives)
    scaled = value.numerator * (2 ** fives * 5 ** twos)
    digits = str(abs(scaled)).zfill(places + 1)
    body = digits[:-places] + "." + digits[-places:] if places else digits
    return ("-" + body) if scaled < 0 else body


def format_interval(interval: Interval, unit: str = "元/年") -> str:
    high = "∞（上不封顶）" if interval.high is None else format_number(interval.high)
    if interval.high is not None and interval.low == interval.high:
        return f"{format_number(interval.low)}{unit}"
    return f"{format_number(interval.low)}-{high}{unit}"
