"""Synthesized tests for the documented Chinese compensation grammar.

Pure-stdlib unittest so ``python -m unittest discover -s tests -v`` runs without
third-party packages. Every monetary assertion goes through Fraction-backed
intervals; no float is ever compared.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from jobtrace.comp_parser import (
    CompensationError, CompensationPlan, Interval, parse_compensation,
)
from jobtrace.compensation import (
    analyze_feasibility, format_interval, format_number,
)
from jobtrace.comparison import (
    INDETERMINATE, OPPORTUNITY, REFUSED, ROBUST, compare_opportunity,
    compare_plans, compare_robust, explain_feasibility,
)

A_TEXT = "20-25K·14薪，目标奖金0-6万，年包35-40万（含奖金）"
B_TEXT = "保底37-38万/年"


class GrammarAndProvenanceTests(unittest.TestCase):
    def test_syntactic_a_keeps_raw_text_and_per_number_spans(self):
        plan = parse_compensation(A_TEXT)
        self.assertEqual(plan.currency, "CNY")
        self.assertTrue(plan.valid, plan.parse_errors)
        kinds = [c.kind for c in plan.clauses]
        self.assertEqual(kinds, ["monthly", "target_bonus", "package"])
        # Every number's span points back at the exact raw substring.
        for clause in plan.clauses:
            for token in clause.numbers:
                self.assertEqual(
                    A_TEXT[token.start:token.end], token.raw,
                    "source span must reconstruct the raw number",
                )
        monthly = plan.clauses_of("monthly")[0]
        self.assertEqual(monthly.months, 14)
        self.assertEqual(
            (monthly.monthly_interval.low, monthly.monthly_interval.high),
            (Fraction(20000), Fraction(25000)),
        )
        # Clause span reconstructs the original clause text.
        self.assertEqual(A_TEXT[monthly.start:monthly.end], "20-25K·14薪")

    def test_unit_sharing_inside_a_range(self):
        plan = parse_compensation("保底37-38万/年")
        clause = plan.clauses_of("guarantee")[0]
        self.assertEqual(
            (clause.interval.low, clause.interval.high),
            (Fraction(370000), Fraction(380000)),
        )
        values = {t.raw: t.value for t in clause.numbers}
        self.assertEqual(values["37"], Fraction(370000))
        self.assertEqual(values["38"], Fraction(380000))

    def test_thousands_comma_is_removed_but_clause_comma_splits(self):
        plan = parse_compensation("月薪20,000-25,000元·14薪，保底30万/年")
        self.assertTrue(plan.valid, plan.parse_errors)
        self.assertEqual([c.kind for c in plan.clauses],
                         ["monthly", "guarantee"])
        monthly = plan.clauses_of("monthly")[0]
        self.assertEqual(
            (monthly.monthly_interval.low, monthly.monthly_interval.high),
            (Fraction(20000), Fraction(25000)),
        )
        self.assertEqual(monthly.months, 14)

    def test_months_outside_12_24_are_reported_not_silently_accepted(self):
        plan = parse_compensation("20K·25薪")
        self.assertTrue(plan.parse_errors)
        self.assertIsNone(plan.clauses_of("monthly")[0].months)

    def test_unrecognized_clause_collects_parse_error(self):
        plan = parse_compensation("20K·13薪，股票若干")
        self.assertTrue(any("无法识别" in err for err in plan.parse_errors))

    def test_empty_text_raises(self):
        with self.assertRaises(CompensationError):
            parse_compensation("")


class FeasibilityTests(unittest.TestCase):
    def test_scenario_a_package_intersects_instead_of_naive_sum(self):
        result = analyze_feasibility(parse_compensation(A_TEXT))
        self.assertTrue(result.feasible)
        # Base 28-35万 and bonus 0-6万 must not be summed into 63-81万.
        self.assertEqual(result.strict_interval,
                         Interval(Fraction(280000), Fraction(350000)))
        self.assertEqual(result.target_interval,
                         Interval(Fraction(0), Fraction(60000)))
        self.assertEqual(result.feasible_interval,
                         Interval(Fraction(350000), Fraction(400000)))
        # The 6万 bonus upper bound is a target, never a guaranteed component.
        target = next(c for c in result.components if c.kind == "target_bonus")
        self.assertFalse(target.guaranteed)
        base = next(c for c in result.components if c.kind == "monthly")
        self.assertTrue(base.guaranteed)

    def test_scenario_b_standalone_guarantee_is_the_feasible_set(self):
        result = analyze_feasibility(parse_compensation(B_TEXT))
        self.assertTrue(result.feasible)
        self.assertEqual(result.feasible_interval,
                         Interval(Fraction(370000), Fraction(380000)))

    def test_infeasible_package_names_the_two_conflicting_constraints(self):
        result = analyze_feasibility(parse_compensation("月薪40K·14薪，年包30万含奖金"))
        self.assertFalse(result.feasible)
        self.assertIsNone(result.feasible_interval)
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual((conflict.left, conflict.right),
                         ("component_sum", "package"))
        # Base alone (56万) already exceeds the 30万 inclusive package.
        self.assertEqual(conflict.left_interval,
                         Interval(Fraction(560000), Fraction(560000)))
        self.assertEqual(conflict.right_interval,
                         Interval(Fraction(300000), Fraction(300000)))

    def test_open_ended_commission_keeps_infinite_upper_bound(self):
        result = analyze_feasibility(parse_compensation("月薪20K·13薪，提成上不封顶"))
        self.assertTrue(result.feasible)
        interval = result.feasible_interval
        self.assertEqual(interval.low, Fraction(260000))
        self.assertIsNone(interval.high, "infinity must not be a big integer")
        self.assertTrue(any(c.open_ended for c in result.plan.clauses))
        self.assertIn("∞", format_interval(interval))

    def test_commission_with_floor_parses_bounded_floor_open_top(self):
        result = analyze_feasibility(parse_compensation("月薪10K·12薪，提成2万起上不封顶"))
        commission = result.plan.clauses_of("commission")[0]
        self.assertEqual(commission.interval.low, Fraction(20000))
        self.assertIsNone(commission.interval.high)

    def test_mixed_currencies_are_parsed_but_flagged(self):
        cny = parse_compensation("20-25K·14薪")
        usd = parse_compensation("USD 8-10K·12薪")
        self.assertEqual(cny.currency, "CNY")
        self.assertEqual(usd.currency, "USD")

    def test_rmb_normalizes_to_cny_for_comparison(self):
        self.assertEqual(parse_compensation("RMB 20K·13薪").currency, "CNY")

    def test_fixed_bonus_is_guaranteed_target_bonus_is_not(self):
        result = analyze_feasibility(
            parse_compensation("20K·13薪，固定奖金1-2万，目标奖金0-3万"))
        guaranteed = {c.kind: c.guaranteed for c in result.components}
        self.assertTrue(guaranteed["fixed_bonus"])
        self.assertFalse(guaranteed["target_bonus"])
        # Strict: 260000-260000 base + 10000-20000 fixed = 270000-280000.
        self.assertEqual(result.strict_interval,
                         Interval(Fraction(270000), Fraction(280000)))
        self.assertEqual(result.feasible_interval,
                         Interval(Fraction(270000), Fraction(310000)))

    def test_parse_error_plan_is_marked_invalid_but_keeps_known_clauses(self):
        plan = parse_compensation("20K·13薪，股票若干")
        self.assertFalse(plan.valid)
        self.assertTrue(any("无法识别" in err for err in plan.parse_errors))
        self.assertEqual(plan.clauses_of("monthly")[0].months, 13)


class DecimalPrecisionTests(unittest.TestCase):
    def test_13_salary_with_decimal_monthly_is_exact(self):
        result = analyze_feasibility(parse_compensation("10.5K·13薪"))
        self.assertEqual(result.feasible_interval.low, Fraction(136500))
        self.assertEqual(format_interval(result.feasible_interval),
                         "136500元/年")

    def test_repeating_decimal_renders_as_explicit_fraction_not_rounded(self):
        plan = parse_compensation("10K·13薪")
        result = analyze_feasibility(plan)
        third = result.feasible_interval.low / 3
        self.assertEqual(third, Fraction(130000, 3))
        self.assertEqual(format_number(third), "130000/3")

    def test_no_float_objects_appear_in_bounds(self):
        result = analyze_feasibility(parse_compensation(A_TEXT))
        for component in result.components:
            self.assertIsInstance(component.interval.low, Fraction)
            if component.interval.high is not None:
                self.assertIsInstance(component.interval.high, Fraction)


class ComparisonContractTests(unittest.TestCase):
    def setUp(self):
        self.a = analyze_feasibility(parse_compensation(A_TEXT))
        self.b = analyze_feasibility(parse_compensation(B_TEXT))

    def test_robust_contract_is_indeterminate_with_counterexample(self):
        verdict = compare_robust(self.a, self.b)
        self.assertEqual(verdict.verdict, INDETERMINATE)
        self.assertFalse(verdict.holds)
        self.assertIn("counterexample_interval", verdict.witness)
        # 35万 (min A) is not strictly above 38万 (max B): concrete witness.
        self.assertIn("350000", verdict.witness["counterexample_interval"])
        self.assertIn("380000", verdict.witness["counterexample_interval"])

    def test_opportunity_contract_holds_with_exact_witness(self):
        verdict = compare_opportunity(self.a, self.b)
        self.assertEqual(verdict.verdict, OPPORTUNITY)
        self.assertTrue(verdict.holds)
        self.assertIn("不等同于保证", verdict.reason)
        self.assertEqual(verdict.witness["a_income"], "400000 CNY")
        self.assertEqual(verdict.witness["b_income"], "370000 CNY")
        self.assertEqual(verdict.witness["difference"], "30000 CNY")

    def test_robust_advantage_holds_when_intervals_are_disjoint(self):
        high = analyze_feasibility(parse_compensation("保底40-42万/年"))
        verdict = compare_robust(high, self.b)
        self.assertEqual(verdict.verdict, ROBUST)
        self.assertEqual(verdict.witness["a_income"], "400000 CNY")
        self.assertEqual(verdict.witness["b_income"], "380000 CNY")

    def test_touching_bounds_are_not_strict_advantage(self):
        left = analyze_feasibility(parse_compensation("保底30-35万/年"))
        right = analyze_feasibility(parse_compensation("保底35-40万/年"))
        self.assertEqual(compare_robust(left, right).verdict, INDETERMINATE)
        self.assertEqual(compare_opportunity(left, right).verdict,
                         INDETERMINATE)

    def test_opportunity_fails_when_a_is_fully_below_b(self):
        low = analyze_feasibility(parse_compensation("保底20-25万/年"))
        verdict = compare_opportunity(low, self.b)
        self.assertEqual(verdict.verdict, INDETERMINATE)
        self.assertIn("250000", verdict.witness["counterexample_interval"])

    def test_infeasible_plan_is_refused_by_both_contracts(self):
        broken = analyze_feasibility(
            parse_compensation("月薪40K·14薪，年包30万含奖金"))
        for contract in (compare_robust(broken, self.b),
                         compare_robust(self.b, broken),
                         compare_opportunity(broken, self.b),
                         compare_opportunity(self.b, broken)):
            self.assertEqual(contract.verdict, REFUSED)
            self.assertIn("无可行解", contract.reason)

    def test_mixed_currency_is_refused_without_rate_table(self):
        usd = analyze_feasibility(parse_compensation("USD 8-10K·12薪"))
        for verdict in (compare_robust(usd, self.b),
                        compare_opportunity(usd, self.b)):
            self.assertEqual(verdict.verdict, REFUSED)
            self.assertIn("币种不同", verdict.reason)

    def test_infinite_upper_bound_robust_indeterminate_opportunity_holds(self):
        opened = analyze_feasibility(
            parse_compensation("月薪20K·13薪，提成上不封顶"))
        robust = compare_robust(opened, self.b)
        opportunity = compare_opportunity(opened, self.b)
        self.assertEqual(robust.verdict, INDETERMINATE)
        self.assertEqual(opportunity.verdict, OPPORTUNITY)
        # Finite witness even though A's top is infinite.
        self.assertTrue(opportunity.witness["a_income"].endswith("CNY"))

    def test_compare_plans_runs_both_contracts_together(self):
        verdicts = compare_plans(parse_compensation(A_TEXT),
                                 parse_compensation(B_TEXT))
        self.assertEqual(verdicts[ROBUST].verdict, INDETERMINATE)
        self.assertEqual(verdicts[OPPORTUNITY].verdict, OPPORTUNITY)

    def test_explanation_payload_carries_components_and_conflicts(self):
        payload = explain_feasibility(self.a)
        self.assertEqual(payload["raw_text"], A_TEXT)
        self.assertEqual(payload["feasible_annual_interval"],
                         "350000-400000元/年")
        self.assertEqual(payload["strict_guaranteed_interval"],
                         "280000-350000元/年")
        self.assertEqual(payload["target_variable_interval"], "0-60000元/年")
        self.assertEqual(payload["package_constraint"], "350000-400000元/年")
        self.assertEqual(payload["conflicts"], [])
        spans = payload["clauses"][0]["source_span"]
        self.assertEqual(A_TEXT[spans[0]:spans[1]], "20-25K·14薪")


if __name__ == "__main__":
    unittest.main()
