"""Two semantically distinct cross-job annual-income comparison contracts.

Both contracts consume exact rational feasible sets produced by
``compensation.analyze_feasibility``; they never read salary_min/salary_max.

* 稳健优势 (robust advantage): every feasible income of A is *strictly* higher
  than every feasible income of B. Equivalent to ``min(A) > max(B)``. Failure
  returns the indeterminate verdict plus a concrete counterexample interval.
* 机会优势 (opportunity advantage): there *exists* a feasible A income strictly
  higher than some feasible B income, i.e. ``max(A) > min(B)``. An infinite
  upper bound always satisfies the existence test; an exact finite witness is
  still returned. This verdict explicitly never means a guaranteed advantage.

Plans that are infeasible or expressed in different currencies are refused:
no ranking is fabricated from conflicting or unconvertible data.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .compensation import (
    CompensationPlan, Feasibility, Interval, analyze_feasibility,
    format_interval, format_number,
)

ROBUST = "robust_advantage"
OPPORTUNITY = "opportunity_advantage"
INDETERMINATE = "indeterminate"
REFUSED = "refused"


@dataclass(frozen=True)
class ComparisonVerdict:
    contract: str
    verdict: str
    reason: str
    witness: dict[str, str]
    feasible: dict[str, str]
    currencies: dict[str, str]

    @property
    def holds(self) -> bool:
        return self.verdict in (ROBUST, OPPORTUNITY)


def _refused(contract: str, reason: str,
             left: Feasibility, right: Feasibility) -> ComparisonVerdict:
    return ComparisonVerdict(
        contract=contract, verdict=REFUSED, reason=reason, witness={},
        feasible={
            "a": "无解" if left.feasible_interval is None
            else format_interval(left.feasible_interval),
            "b": "无解" if right.feasible_interval is None
            else format_interval(right.feasible_interval),
        },
        currencies={"a": left.plan.currency, "b": right.plan.currency},
    )


def _guard(contract: str, left: Feasibility, right: Feasibility) -> ComparisonVerdict | None:
    if left.plan.currency != right.plan.currency:
        return _refused(
            contract,
            f"币种不同（{left.plan.currency} vs {right.plan.currency}），"
            "缺少本地可用换算依据，不可比较",
            left, right,
        )
    if left.feasible_interval is None or right.feasible_interval is None:
        return _refused(contract, "至少一方薪酬约束无可行解，拒绝比较", left, right)
    return None


def _witness_pair(a_value: Fraction, b_value: Fraction,
                  currency: str) -> dict[str, str]:
    return {
        "a_income": f"{format_number(a_value)} {currency}",
        "b_income": f"{format_number(b_value)} {currency}",
        "difference": f"{format_number(a_value - b_value)} {currency}",
    }


def compare_robust(left: Feasibility, right: Feasibility) -> ComparisonVerdict:
    """A 优于 B only when *every* feasible A income beats *every* B income."""

    refused = _guard(ROBUST, left, right)
    if refused is not None:
        return refused
    a, b = left.feasible_interval, right.feasible_interval
    feasible = {"a": format_interval(a), "b": format_interval(b)}
    currencies = {"a": left.plan.currency, "b": right.plan.currency}
    if b.high is None:
        return ComparisonVerdict(
            ROBUST, INDETERMINATE,
            "B 的可行收入上界为无穷，A 不可能在每个取值上都严格高于 B",
            {"counterexample": f"B 存在高于 A 任意有限取值的收入（{feasible['b']}）"},
            feasible, currencies,
        )
    if a.low > b.high:
        return ComparisonVerdict(
            ROBUST, ROBUST,
            "A 的最低可行收入仍严格高于 B 的最高可行收入",
            _witness_pair(a.low, b.high, left.plan.currency), feasible, currencies,
        )
    overlap_high = b.high if a.high is None else (
        a.high if b.high is None else min(a.high, b.high))
    return ComparisonVerdict(
        ROBUST, INDETERMINATE,
        "A 的最低可行收入未严格高于 B 的最高可行收入，稳健优势不成立",
        {"counterexample_interval":
         f"A={format_number(a.low)}元/年 与 B={format_number(overlap_high)}元/年 时 A 不占优"},
        feasible, currencies,
    )


def compare_opportunity(left: Feasibility, right: Feasibility) -> ComparisonVerdict:
    """A has *some* feasible income strictly above *some* feasible B income."""

    refused = _guard(OPPORTUNITY, left, right)
    if refused is not None:
        return refused
    a, b = left.feasible_interval, right.feasible_interval
    feasible = {"a": format_interval(a), "b": format_interval(b)}
    currencies = {"a": left.plan.currency, "b": right.plan.currency}
    if a.high is None:
        witness_value = b.low + Fraction(1)
        return ComparisonVerdict(
            OPPORTUNITY, OPPORTUNITY,
            "A 的可行收入上不封顶，必然存在高于 B 某可行收入的取值；"
            "机会优势不等同于保证优势",
            _witness_pair(witness_value, b.low, left.plan.currency),
            feasible, currencies,
        )
    if a.high > b.low:
        return ComparisonVerdict(
            OPPORTUNITY, OPPORTUNITY,
            "存在满足全部约束的 A 收入高于某个可行 B 收入；"
            "机会优势不等同于保证优势",
            _witness_pair(a.high, b.low, left.plan.currency), feasible, currencies,
        )
    return ComparisonVerdict(
        OPPORTUNITY, INDETERMINATE,
        "A 的最高可行收入仍不高于 B 的最低可行收入，机会优势不成立",
        {"counterexample_interval":
         f"A 最高 {format_number(a.high)}元/年 ≤ B 最低 {format_number(b.low)}元/年"},
        feasible, currencies,
    )


def compare_plans(plan_a: CompensationPlan, plan_b: CompensationPlan) -> dict[str, ComparisonVerdict]:
    """Convenience entry point: parse once, evaluate both contracts."""

    left, right = analyze_feasibility(plan_a), analyze_feasibility(plan_b)
    return {
        ROBUST: compare_robust(left, right),
        OPPORTUNITY: compare_opportunity(left, right),
    }


def explain_feasibility(result: Feasibility) -> dict[str, object]:
    """JSON-ready explanation with raw text, spans and exact bounds."""

    plan = result.plan
    return {
        "raw_text": plan.raw,
        "currency": plan.currency,
        "valid": plan.valid,
        "parse_errors": list(plan.parse_errors),
        "clauses": [
            {
                "kind": clause.kind,
                "text": clause.text,
                "source_span": [clause.start, clause.end],
                "open_ended": clause.open_ended,
                "annual_interval": None if clause.interval is None
                else format_interval(clause.interval),
                "numbers": [
                    {"value": format_number(token.value), "raw": token.raw,
                     "source_span": [token.start, token.end]}
                    for token in clause.numbers
                ],
            }
            for clause in plan.clauses
        ],
        "components": [
            {"kind": c.kind, "label": c.label, "guaranteed": c.guaranteed,
             "interval": format_interval(c.interval),
             "sources": [clause.text for clause in c.sources]}
            for c in result.components
        ],
        "strict_guaranteed_interval": format_interval(result.strict_interval),
        "target_variable_interval": format_interval(result.target_interval),
        "package_constraint": None if result.package_interval is None
        else format_interval(result.package_interval),
        "feasible": result.feasible,
        "feasible_annual_interval": None if result.feasible_interval is None
        else format_interval(result.feasible_interval),
        "conflicts": [
            {"left": conflict.left, "right": conflict.right,
             "left_interval": format_interval(conflict.left_interval),
             "right_interval": format_interval(conflict.right_interval),
             "detail": conflict.detail}
            for conflict in result.conflicts
        ],
    }
