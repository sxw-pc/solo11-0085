"""Read-only audit of a recorded detail sampling plan."""

from __future__ import annotations

from typing import Any

from .sampling import SamplingFrame, SamplingSelection, _number, plan_probability


def audit_selection(frame: SamplingFrame, selection: SamplingSelection,
                    seed: int | None = None) -> dict[str, Any]:
    """Recompute a selection read-only and flag stale frame digests, seeds, or weights."""
    issues: list[str] = []
    if selection.run_id != frame.run_id:
        issues.append("run_id differs from the current frame")
    if selection.frame_digest != frame.digest:
        issues.append("frame digest differs: list response/cards changed; "
                      "recorded sample weights are stale")
    expected = {name: len(ids) for name, ids in frame.strata().items()}
    if dict(selection.stratum_sizes) != expected:
        issues.append("stratum sizes differ from the current frame")
    if selection.contract == "probability":
        if selection.seed is None:
            issues.append("probability selection has no seed")
        elif seed is not None and int(seed) != int(selection.seed):
            issues.append("seed differs from the recorded selection; "
                          "weights must not be reused")
        else:
            replay = plan_probability(frame, selection.budget, int(selection.seed))
            if replay.cards != selection.cards or replay.allocations != selection.allocations:
                issues.append("selection does not match deterministic recomputation")
        for card in selection.cards:
            if card.allocation <= 0 or card.stratum_size <= 0:
                issues.append(f"invalid allocation for {card.external_id}")
            elif card.inclusion_probability is None or abs(
                    card.inclusion_probability - card.allocation / card.stratum_size) > 1e-12:
                issues.append(f"invalid inclusion probability for {card.external_id}")
        if not selection.all_strata_positive:
            issues.append("at least one stratum has zero probability; "
                          "no unbiased whole-page proportion")
    elif selection.contract == "targeted":
        known = set(frame.ids())
        missing = [card_id for card_id in selection.selected_ids if card_id not in known]
        if missing:
            issues.append("targeted selection contains IDs outside the frame: "
                          + ", ".join(missing))
        if any(card.inclusion_probability is not None for card in selection.cards):
            issues.append("targeted selection must not carry probability weights")
    else:
        issues.append(f"unknown contract: {selection.contract}")
    return {"ok": not issues, "issues": issues, "frame_digest": frame.digest,
            "selected_ids": list(selection.selected_ids)}


def selection_payload(selection: SamplingSelection) -> dict[str, Any]:
    """Stable JSON projection persisted with the run for later review."""
    return {
        "contract": selection.contract, "run_id": selection.run_id,
        "frame_digest": selection.frame_digest, "seed": selection.seed,
        "budget": selection.budget, "allocation_rule": selection.allocation_rule,
        "allocations": dict(selection.allocations),
        "stratum_sizes": dict(selection.stratum_sizes),
        "uncovered_strata": dict(selection.uncovered_strata),
        "uncovered_ids": list(selection.uncovered_ids),
        "unresolved_target_ids": list(selection.unresolved_target_ids),
        "duplicate_ids": list(selection.duplicate_ids),
        "duplicate_count": selection.duplicate_count,
        "selected": [
            {"external_id": card.external_id, "stratum": card.stratum, "order": card.order,
             "stratum_size": card.stratum_size, "allocation": card.allocation,
             "inclusion_probability": _number(card.inclusion_probability)}
            for card in selection.cards
        ],
        "warnings": list(selection.warnings),
    }
