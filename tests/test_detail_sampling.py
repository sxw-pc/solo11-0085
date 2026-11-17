"""Bounded detail sampling: targeted vs. probability contracts and audit rules."""

from __future__ import annotations

import unittest

from jobtrace.errors import ConfigError
from jobtrace.models import ListItem
from jobtrace.sampling import (
    SamplingRequest, build_frame, plan_probability, plan_targeted,
)
from jobtrace.sampling_audit import audit_selection, selection_payload
from jobtrace.sampling_eval import DetailOutcome, audit_payload, evaluate
from jobtrace.sampling_eval import POSITIVE_VALUE


def card(card_id: str, company: str, employment: str | None = None) -> ListItem:
    return ListItem(
        external_id=card_id, title=f"岗位 {card_id}", company=company,
        location="上海 · 徐汇区", salary="20-30K", detail_url=f"http://x/jobs/{card_id}",
    )


def four_cards() -> list[ListItem]:
    return [card("A1", "公司甲"), card("A2", "公司甲"),
            card("B1", "公司乙"), card("B2", "公司乙")]


def frame(cards=None, *, digest_url="http://x/jobs?page=1", body="page-one"):
    return build_frame(
        "run-4", cards or four_cards(),
        list_documents=[(digest_url, body)], list_cards=len(cards or four_cards()),
    )


def outcomes(mapping: dict[str, tuple[str, str | None]]) -> dict[str, DetailOutcome]:
    return {card_id: DetailOutcome(card_id, status, employment)
            for card_id, (status, employment) in mapping.items()}


def seed_selecting(frame_obj, ids: set[str]) -> int:
    for seed in range(200):
        selection = plan_probability(frame_obj, len(ids), seed)
        if set(selection.selected_ids) == ids:
            return seed
    raise AssertionError(f"no seed selects {sorted(ids)}")


class TargetedContractTest(unittest.TestCase):
    def test_targeted_fetches_only_authorised_ids_and_gives_no_page_ratio(self):
        frozen = frame()
        selection = plan_targeted(frozen, ["A1", "A2"], 2)
        self.assertEqual(selection.selected_ids, ("A1", "A2"))
        self.assertEqual(selection.allocations, {"公司甲": 2, "公司乙": 0})
        self.assertEqual(selection.uncovered_strata, {"公司乙": 2})
        self.assertTrue(all(card.inclusion_probability is None for card in selection.cards))
        evaluation = evaluate(selection, frozen, outcomes(
            {"A1": ("ok", "全职"), "A2": ("ok", "全职")}))
        self.assertIsNone(evaluation["estimate"])
        self.assertIsNone(evaluation["bounds"])
        audit = audit_payload(frozen, selection, evaluation)
        self.assertEqual(audit["claims"]["coverage_scope"], "targeted_ids_only")
        self.assertFalse(audit["claims"]["whole_page_unbiased_proportion"])
        self.assertEqual(audit["uncovered_ids"], ["B1", "B2"])
        self.assertEqual(audit["uncovered_strata"], {"公司乙": 2})
        self.assertTrue(audit["audit"]["ok"])
        self.assertTrue(selection_payload(selection)["selected"])

    def test_targeted_ignores_unknown_and_budget_deferred_ids(self):
        frozen = frame()
        selection = plan_targeted(frozen, ["A1", "ZZ", "A2", "A1"], 1)
        self.assertEqual(selection.selected_ids, ("A1",))
        self.assertEqual(plan_targeted(frozen, ["B1", "A1"], 2).selected_ids,
                         ("B1", "A1"))
        self.assertEqual(selection.unresolved_target_ids, ("ZZ",))
        self.assertTrue(any("deferred" in warning for warning in selection.warnings))

    def test_targeted_requires_at_least_one_id(self):
        with self.assertRaises(ConfigError):
            SamplingRequest("targeted")
        with self.assertRaises(ConfigError):
            SamplingRequest("probability")


class ProbabilityContractTest(unittest.TestCase):
    def test_seeded_stratified_sample_estimates_interns_as_design_weight(self):
        frozen = frame()
        seed = seed_selecting(frozen, {"A1", "B1"})
        selection = plan_probability(frozen, 2, seed)
        self.assertEqual(selection.allocations, {"公司甲": 1, "公司乙": 1})
        for selected in selection.cards:
            self.assertEqual(selected.allocation, 1)
            self.assertEqual(selected.inclusion_probability, 0.5)
        evaluation = evaluate(selection, frozen, outcomes(
            {"A1": ("ok", "全职"), "B1": ("ok", POSITIVE_VALUE)}))
        estimate = evaluation["estimate"]
        self.assertEqual(estimate["intern_count"], 2.0)
        self.assertEqual(estimate["proportion"], 0.5)
        self.assertIn("design-weighted", estimate["label"])
        self.assertEqual(evaluation["verified_positive"], 1)
        self.assertEqual(evaluation["bounds"]["intern_count"], [1, 3])
        self.assertIsNone(evaluation["variance"])
        self.assertFalse(evaluation["variance_estimable"])
        self.assertIn("not estimable", evaluation["variance_reason"])
        self.assertIsNone(evaluation["confidence_interval"])
        audit = audit_payload(frozen, selection, evaluation)
        self.assertEqual(audit["seed"], seed)
        self.assertEqual(audit["counts"]["list_discovered"], 4)
        self.assertEqual(audit["counts"]["detail_attempted"], 2)
        self.assertEqual(audit["counts"]["verified"], 2)
        self.assertEqual(audit["counts"]["not_attempted"], 2)
        self.assertEqual(audit["claims"]["coverage_scope"], "selected_probability_sample")
        self.assertTrue(audit["claims"]["whole_page_unbiased_proportion"])
        self.assertFalse(audit["claims"]["details_complete"])
        self.assertEqual(audit["counts"]["not_attempted"], 2)

    def test_same_seed_replays_and_a_changed_seed_does_not_reuse_weights(self):
        frozen = frame()
        first = plan_probability(frozen, 2, 11)
        second = plan_probability(frozen, 2, 11)
        self.assertEqual(first, second)
        self.assertTrue(audit_selection(frozen, first, seed=11)["ok"])
        mismatch = audit_selection(frozen, first, seed=12)
        self.assertFalse(mismatch["ok"])
        self.assertTrue(any("seed differs" in issue for issue in mismatch["issues"]))

    def test_changed_list_response_invalidates_recorded_sample(self):
        original = frame()
        selection = plan_probability(original, 2, 5)
        changed = frame(body="page-one-revised")
        self.assertNotEqual(original.digest, changed.digest)
        audit = audit_selection(changed, selection)
        self.assertFalse(audit["ok"])
        self.assertTrue(any("frame digest differs" in issue for issue in audit["issues"]))

    def test_budget_below_stratum_count_leaves_some_strata_at_zero(self):
        cards = [card("A1", "甲"), card("A2", "甲"), card("B1", "乙"),
                 card("B2", "乙"), card("C1", "丙")]
        frozen = frame(cards)
        selection = plan_probability(frozen, 2, 3)
        self.assertEqual(sum(selection.allocations.values()), 2)
        self.assertIn(0, selection.allocations.values())
        self.assertFalse(selection.all_strata_positive)
        self.assertTrue(any("no positive inclusion probability" in warning
                            for warning in selection.warnings))
        evaluation = evaluate(selection, frozen, outcomes(
            {card_id: ("ok", "全职") for card_id in selection.selected_ids}))
        self.assertIsNone(evaluation["estimate"]["intern_count"])
        self.assertFalse(audit_selection(frozen, selection)["ok"])

    def test_uneven_strata_allocate_one_each_then_largest_capacity(self):
        cards = [card("A1", "甲"), card("A2", "甲"), card("A3", "甲"),
                 card("B1", "乙"), card("C1", "丙")]
        frozen = frame(cards)
        selection = plan_probability(frozen, 4, 7)
        self.assertEqual(selection.allocations, {"甲": 2, "乙": 1, "丙": 1})
        self.assertEqual(selection.stratum_sizes, {"甲": 3, "乙": 1, "丙": 1})
        probabilities = {c.external_id: c.inclusion_probability for c in selection.cards}
        self.assertEqual(probabilities["B1"], 1.0)
        self.assertEqual(sum(probabilities.values()), 2 / 3 + 2 / 3 + 1.0 + 1.0)

    def test_duplicate_ids_in_the_listing_are_deduplicated_once(self):
        duplicates = four_cards() + [card("A1", "公司甲")]
        frozen = build_frame("run-4", duplicates, list_documents=[("u", "d")],
                             list_cards=5, duplicate_ids=["A1"])
        self.assertEqual(len(frozen.cards), 4)
        self.assertEqual(frozen.duplicate_count, 1)
        selection = plan_probability(frozen, 2, 4)
        self.assertEqual(len(set(selection.selected_ids)), len(selection.selected_ids))
        audit = audit_payload(frozen, selection, evaluate(selection, frozen, outcomes(
            {card_id: ("ok", "全职") for card_id in selection.selected_ids})))
        self.assertEqual(audit["counts"]["list_discovered"], 5)
        self.assertEqual(audit["counts"]["duplicates_removed"], 1)
        self.assertEqual(audit["frame"]["unique_cards"], 4)

    def test_full_frame_probability_sample_can_claim_design_estimate(self):
        cards = [card("A1", "甲"), card("B1", "乙")]
        frozen = frame(cards)
        selection = plan_probability(frozen, 2, 1)
        evaluation = evaluate(selection, frozen, outcomes(
            {"A1": ("ok", "全职"), "B1": ("ok", POSITIVE_VALUE)}))
        audit = audit_payload(frozen, selection, evaluation)
        self.assertTrue(selection.covers_frame())
        self.assertTrue(audit["claims"]["whole_page_unbiased_proportion"])
        self.assertEqual(audit["estimate"]["intern_count"], 1.0)


class FailureIsUnknownTest(unittest.TestCase):
    def test_404_missing_field_and_unknown_type_are_not_non_interns(self):
        cards = [card("A1", "甲"), card("B1", "乙")]
        frozen = frame(cards)
        selection = plan_probability(frozen, 2, 1)
        evaluation = evaluate(selection, frozen, {
            "A1": DetailOutcome("A1", "fetch_error", error="HTTP 404", status_code=404),
            "B1": DetailOutcome("B1", "ok", employment_type=None),
        })
        self.assertEqual(evaluation["verified"], 0)
        self.assertEqual(evaluation["verified_positive"], 0)
        self.assertEqual(evaluation["unknown_field"], 1)
        self.assertEqual(evaluation["detail_failed"], 1)
        self.assertIsNone(evaluation["estimate"]["intern_count"])
        self.assertEqual(evaluation["bounds"]["intern_count"], [0, 2])
        audit = audit_payload(frozen, selection, evaluation)
        self.assertFalse(audit["claims"]["whole_page_unbiased_proportion"])
        self.assertTrue(any("failed" in str(entry["variance_reason"])
                            for entry in audit["strata"]))

    def test_missing_required_fields_count_as_failure_not_negative(self):
        frozen = frame([card("A1", "甲")])
        selection = plan_targeted(frozen, ["A1"], 1)
        evaluation = evaluate(selection, frozen, {
            "A1": DetailOutcome("A1", "missing_required", error="title")})
        self.assertEqual(evaluation["detail_failed"], 1)
        self.assertEqual(evaluation["verified"], 0)


if __name__ == "__main__":
    unittest.main()


class AuditIntegrityTest(unittest.TestCase):
    def test_probability_weights_are_not_recomputed_from_successful_fetches(self):
        frozen = frame()
        selection = plan_probability(frozen, 2, 1)
        before = {card.external_id: card.inclusion_probability
                  for card in selection.cards}
        evaluation = evaluate(selection, frozen, {
            selection.cards[0].external_id: DetailOutcome(
                selection.cards[0].external_id, "fetch_error", error="HTTP 404"),
            selection.cards[1].external_id: DetailOutcome(
                selection.cards[1].external_id, "ok", "实习"),
        })
        after = {row["external_id"]: row["inclusion_probability"]
                 for row in evaluation["cards"]}
        self.assertEqual(before, after)
        self.assertIsNone(evaluation["estimate"]["intern_count"])
        self.assertTrue(audit_selection(frozen, selection)["ok"])

    def test_tampered_weight_or_targeted_weight_wrapper_is_flagged(self):
        from dataclasses import replace
        frozen = frame()
        selection = plan_probability(frozen, 2, 1)
        tampered_card = replace(selection.cards[0], inclusion_probability=0.9)
        tampered = replace(selection, cards=(tampered_card, *selection.cards[1:]))
        audit = audit_selection(frozen, tampered)
        self.assertFalse(audit["ok"])
        self.assertTrue(any("inclusion probability" in issue for issue in audit["issues"]))
        targeted = plan_targeted(frozen, ["A1"], 1)
        wrapped_card = replace(targeted.cards[0], inclusion_probability=0.5)
        wrapped = replace(targeted, cards=(wrapped_card,))
        audit = audit_selection(frozen, wrapped)
        self.assertFalse(audit["ok"])
        self.assertTrue(any("must not carry probability weights" in issue
                            for issue in audit["issues"]))
