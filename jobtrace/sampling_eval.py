"""Design-based evaluation and persisted audit payload for detail sampling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .sampling import POSITIVE_VALUE, SamplingFrame, SamplingSelection, _number
from .sampling_audit import audit_selection


@dataclass(frozen=True)
class DetailOutcome:
    external_id: str
    status: str = "missing"
    employment_type: str | None = None
    error: str | None = None
    status_code: int | None = None

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.employment_type is not None

    @property
    def positive(self) -> bool:
        return self.usable and self.employment_type == POSITIVE_VALUE


def _stratum(name: str, size: int, allocated: int, cards: Sequence[Any],
             outcomes: Mapping[str, DetailOutcome]) -> dict[str, Any]:
    selected = [card for card in cards if card.stratum == name]
    usable = [outcomes[card.external_id] for card in selected
              if card.external_id in outcomes and outcomes[card.external_id].usable]
    positives = sum(1 for outcome in usable if outcome.positive)
    failed = len(selected) - len(usable)
    n = len(selected)
    estimate = variance = None
    variance_reason = None
    if allocated == 0:
        variance_reason = f"stratum {name!r} has zero inclusion probability"
    elif failed:
        variance_reason = (f"stratum {name!r} has failed or unknown selected outcomes; "
                           "no non-response imputation is allowed")
    else:
        estimate = size * positives / n
        if allocated == size:
            variance = 0.0
        elif n >= 2:
            mean = positives / n
            sample_variance = sum(
                ((1 if outcome.positive else 0) - mean) ** 2 for outcome in usable
            ) / (n - 1)
            variance = size ** 2 * (1 - n / size) * sample_variance / n
        else:
            variance_reason = (f"stratum {name!r} has n=1 and N={size}; "
                               "the within-stratum variance is not estimable")
    return {
        "stratum": name, "size": size, "allocated": allocated, "selected": n,
        "inclusion_probability": _number(allocated / size if size else 0.0),
        "verified": len(usable), "verified_positive": positives,
        "unknown": sum(1 for card in selected if card.external_id in outcomes
                       and outcomes[card.external_id].status == "ok"
                       and outcomes[card.external_id].employment_type is None),
        "failed": failed, "not_attempted": size - allocated,
        "estimate": _number(estimate), "variance": _number(variance),
        "variance_reason": variance_reason,
        "bounds": [positives, positives + (size - len(usable))],
    }


def evaluate(selection: SamplingSelection, frame: SamplingFrame,
             outcomes: Sequence[DetailOutcome] | Mapping[str, DetailOutcome]) -> dict[str, Any]:
    """Turn selected detail outcomes into verified counts, bounds, and estimates."""
    by_id = dict(outcomes) if isinstance(outcomes, Mapping) else {
        outcome.external_id: outcome for outcome in outcomes
    }
    rows = []
    attempted = succeeded = verified = verified_positive = unknown = 0
    for selected in selection.cards:
        outcome = by_id.get(selected.external_id, DetailOutcome(selected.external_id))
        attempted += 1
        succeeded += int(outcome.status == "ok")
        verified += int(outcome.usable)
        verified_positive += int(outcome.positive)
        unknown += int(outcome.status == "ok" and outcome.employment_type is None)
        rows.append({
            "external_id": selected.external_id, "stratum": selected.stratum,
            "order": selected.order, "stratum_size": selected.stratum_size,
            "allocation": selected.allocation,
            "inclusion_probability": _number(selected.inclusion_probability),
            "outcome": outcome.status, "employment_type": outcome.employment_type,
            "verified": outcome.usable, "error": outcome.error,
        })
    result: dict[str, Any] = {
        "detail_attempted": attempted, "detail_succeeded": succeeded,
        "detail_failed": attempted - succeeded,
        "verified": verified, "verified_positive": verified_positive,
        "unknown_field": unknown, "not_attempted": len(frame.cards) - attempted,
        "cards": rows,
        "missing_policy": ("404, missing required fields, and unknown employment_type "
                           "are unknown, never counted as non-intern"),
        "strata": [],
    }
    if selection.contract == "targeted":
        result["estimate"] = None
        result["bounds"] = None
        result["variance"] = result["confidence_interval"] = None
        result["variance_estimable"] = False
        result["reason"] = ("targeted contract: verified IDs only; "
                            "no whole-page inference")
        result["strata"] = [{
            "stratum": name, "size": size,
            "verified": sum(1 for row in rows if row["stratum"] == name and row["verified"]),
            "verified_positive": sum(1 for row in rows if row["stratum"] == name
                                     and row["employment_type"] == POSITIVE_VALUE
                                     and row["verified"]),
        } for name, size in sorted(selection.stratum_sizes.items())]
        return result
    strata = [_stratum(name, size, selection.allocations.get(name, 0),
                       selection.cards, by_id)
              for name, size in sorted(selection.stratum_sizes.items())]
    result["strata"] = strata
    if all(entry["estimate"] is not None for entry in strata) and selection.all_strata_positive:
        total = sum(entry["estimate"] for entry in strata)
        result["estimate"] = {
            "field": "employment_type", "positive_value": POSITIVE_VALUE,
            "intern_count": _number(total),
            "proportion": _number(total / len(frame.cards)) if frame.cards else None,
            "label": "design-weighted estimate; not a verified count",
            "scope": "whole_frame", "design_based": True,
        }
    else:
        reason = ("at least one stratum has zero inclusion probability"
                  if not selection.all_strata_positive else
                  "selected sample is incomplete; no non-response imputation")
        result["estimate"] = {
            "field": "employment_type", "positive_value": POSITIVE_VALUE,
            "intern_count": None, "proportion": None, "scope": "none",
            "design_based": True, "reason": reason,
        }
    lower = sum(entry["bounds"][0] for entry in strata)
    upper = sum(entry["bounds"][1] for entry in strata)
    total_cards = len(frame.cards)
    result["bounds"] = {
        "intern_count": [lower, upper],
        "proportion": [_number(lower / total_cards) if total_cards else None,
                       _number(upper / total_cards) if total_cards else None],
        "scope": "whole_frame",
        "note": "provable binary bounds; unobserved units may be 0 or 1",
    }
    variances = [entry["variance"] for entry in strata]
    if all(value is not None for value in variances) and selection.all_strata_positive:
        result["variance"] = _number(sum(variances))
        result["variance_estimable"] = True
        result["variance_reason"] = None
    else:
        result["variance"] = None
        result["variance_estimable"] = False
        result["variance_reason"] = next(
            (entry["variance_reason"] for entry in strata if entry["variance_reason"]),
            "variance is not estimable for this design")
    result["confidence_interval"] = None
    result["ci_reason"] = ("not reported: one or more strata lack a variance estimate "
                           "or the normal approximation is not justified")
    return result


def _payload(frame: SamplingFrame, selection: SamplingSelection,
             evaluation: Mapping[str, Any], *, scope: str, unbiased: bool,
             complete: bool, note: str,
             request_stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "contract": selection.contract, "run_id": frame.run_id,
        "stratum_field": frame.stratum_field,
        "frame": {
            "digest": frame.digest, "list_cards": frame.list_cards,
            "unique_cards": len(frame.cards), "duplicate_ids": list(frame.duplicate_ids),
            "duplicate_count": frame.duplicate_count,
            "strata": {name: len(ids) for name, ids in frame.strata().items()},
            "list_documents": [{"url": url, "sha256": digest}
                               for url, digest in frame.list_documents],
        },
        "seed": selection.seed, "budget": selection.budget,
        "allocation_rule": selection.allocation_rule,
        "allocations": dict(selection.allocations),
        "uncovered_strata": dict(selection.uncovered_strata),
        "uncovered_ids": list(selection.uncovered_ids),
        "unresolved_target_ids": list(selection.unresolved_target_ids),
        "selected": evaluation["cards"],
        "counts": {
            "list_discovered": frame.list_cards, "list_unique": len(frame.cards),
            "duplicates_removed": frame.duplicate_count,
            "detail_attempted": evaluation["detail_attempted"],
            "detail_succeeded": evaluation["detail_succeeded"],
            "detail_failed": evaluation["detail_failed"],
            "verified": evaluation["verified"],
            "verified_positive": evaluation["verified_positive"],
            "unknown_field": evaluation["unknown_field"],
            "not_attempted": evaluation["not_attempted"],
        },
        "estimate": evaluation.get("estimate"), "bounds": evaluation.get("bounds"),
        "variance": evaluation.get("variance"),
        "variance_estimable": evaluation.get("variance_estimable", False),
        "variance_reason": evaluation.get("variance_reason"),
        "confidence_interval": evaluation.get("confidence_interval"),
        "ci_reason": evaluation.get("ci_reason"),
        "strata": evaluation["strata"],
        "missing_policy": evaluation["missing_policy"],
        "claims": {
            "details_complete": complete, "coverage_scope": scope,
            "whole_page_unbiased_proportion": unbiased, "note": note,
        },
        "warnings": list(selection.warnings),
        "audit": audit_selection(frame, selection),
        "request_stats": dict(request_stats or {}),
    }


def audit_payload(frame: SamplingFrame, selection: SamplingSelection,
                  evaluation: Mapping[str, Any],
                  request_stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the persisted, reviewable contract/frame/selection/estimate record."""
    estimate = evaluation.get("estimate")
    selected_complete = (evaluation["detail_attempted"] > 0
                         and evaluation["verified"] == evaluation["detail_attempted"])
    if selection.contract == "targeted":
        return _payload(
            frame, selection, evaluation, scope="targeted_ids_only", unbiased=False,
            complete=False,
            note=("targeted audit states the verified fields of the authorised IDs "
                  "and the uncovered list; it is not a whole-page estimate"),
            request_stats=request_stats)
    details_complete = selection.covers_frame() and selected_complete
    if selection.all_strata_positive and selected_complete:
        scope = "whole_frame" if selection.covers_frame() else "selected_probability_sample"
        unbiased = bool(estimate and estimate.get("intern_count") is not None)
        note = ("design-weighted estimate from a probability sample with positive "
                "inclusion probability in every stratum; not a full verification")
    elif not selection.all_strata_positive:
        scope, unbiased = "partial_probability_sample", False
        note = ("at least one stratum has zero inclusion probability; "
                "no whole-page unbiased proportion is claimed")
    else:
        scope, unbiased = "selected_probability_sample", False
        note = ("selected details failed or are unknown; no non-response imputation "
                "and no whole-page unbiased proportion is claimed")
    return _payload(frame, selection, evaluation, scope=scope, unbiased=unbiased,
                    complete=details_complete, note=note, request_stats=request_stats)
