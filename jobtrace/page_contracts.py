"""Collection contracts over proven list-page evidence.

``fixed_snapshot`` emits one complete job list only if a single epoch's whole
chain is proven; otherwise the round is incomplete with missing/conflict
witnesses. ``live_observation`` keeps partial observations per snapshot id and
emits only proven coverage ranges; segments are never merged into a whole job
set and never drive missing/closed lifecycle decisions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .page_epoch import EpochChain, EvidenceLedger

CONTRACTS = ("fixed_snapshot", "live_observation")


@dataclass
class RoundVerdict:
    contract: str
    complete: bool
    stop_reason: str
    snapshot_id: str | None
    items: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    witnesses: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""

    @property
    def claims_all_jobs(self) -> bool:
        """Only a proven fixed-snapshot chain may claim the whole job set."""

        return self.complete and self.contract == "fixed_snapshot"

    def as_dict(self) -> dict[str, Any]:
        return {"contract": self.contract, "complete": self.complete,
                "stop_reason": self.stop_reason, "snapshot_id": self.snapshot_id,
                "items": list(self.items), "segments": self.segments,
                "witnesses": self.witnesses, "detail": self.detail,
                "claims_all_jobs": self.claims_all_jobs}


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _segment(epoch: EpochChain) -> dict[str, Any]:
    payload = epoch.as_dict()
    ranges = payload["coverage_ranges"]
    payload["segment_items"] = _dedupe(
        item for start, end in ranges for item in payload["range_items"][f"{start}-{end}"])
    return payload


def _witnesses(ledger: EvidenceLedger, chosen: EpochChain | None) -> list[dict[str, Any]]:
    collected = [item.as_dict() for item in ledger.witnesses]
    if chosen is not None:
        collected.extend(item.as_dict() for item in chosen.witnesses)
    return collected


def fixed_snapshot_verdict(ledger: EvidenceLedger, *, expected_snapshot: str | None = None,
                           stop_reason: str | None = None) -> RoundVerdict:
    """Fixed contract: only a whole proven chain may emit a job list."""

    chosen = ledger.by_epoch.get(expected_snapshot) if expected_snapshot else None
    if chosen is None:
        complete = ledger.complete_epochs()
        chosen = complete[0] if len(complete) == 1 else None
    witnesses = _witnesses(ledger, chosen)
    segments = [_segment(epoch) for epoch in ledger.by_epoch.values()]
    if chosen is not None and chosen.complete:
        return RoundVerdict(
            contract="fixed_snapshot", complete=True,
            stop_reason=stop_reason or "complete_chain",
            snapshot_id=chosen.snapshot_id,
            items=chosen.items_for(chosen.indices), segments=segments,
            witnesses=witnesses, detail="single snapshot chain proven page 1..N")
    reason = stop_reason or (
        "snapshot_switch" if len(ledger.by_epoch) > 1 else "incomplete_chain")
    if chosen is not None:
        detail = (f"epoch {chosen.snapshot_id} missing pages {chosen.missing_pages()}"
                  if chosen.total_pages is not None else
                  f"epoch {chosen.snapshot_id} has no total_pages declaration")
    else:
        detail = "no single snapshot chain spans page 1..N"
    return RoundVerdict(
        contract="fixed_snapshot", complete=False, stop_reason=reason,
        snapshot_id=chosen.snapshot_id if chosen else None, segments=segments,
        witnesses=witnesses, detail=detail)


def live_observation_verdict(ledger: EvidenceLedger, *,
                             stop_reason: str | None = None) -> RoundVerdict:
    """Live contract: per-snapshot partial segments, never merged."""

    segments = [_segment(epoch) for epoch in ledger.by_epoch.values()]
    sole_complete = len(ledger.by_epoch) == 1 and next(
        iter(ledger.by_epoch.values())).complete
    return RoundVerdict(
        contract="live_observation", complete=sole_complete,
        stop_reason=stop_reason or (
            "proven_single_snapshot" if sole_complete else "partial_epochs"),
        snapshot_id=next(iter(ledger.by_epoch)) if len(ledger.by_epoch) == 1 else None,
        items=[], segments=segments,
        witnesses=[item.as_dict() for item in ledger.witnesses],
        detail="per-snapshot coverage only; segments are never merged")


def verdict_for(contract: str, ledger: EvidenceLedger, *,
                expected_snapshot: str | None = None,
                stop_reason: str | None = None) -> RoundVerdict:
    if contract not in CONTRACTS:
        raise ValueError(f"unknown collection contract: {contract}")
    if contract == "fixed_snapshot":
        return fixed_snapshot_verdict(ledger, expected_snapshot=expected_snapshot,
                                      stop_reason=stop_reason)
    return live_observation_verdict(ledger, stop_reason=stop_reason)
