"""Auditable detail sampling plans for bounded crawls.

The frame is the complete set of de-duplicated list cards observed in one run,
bound to the list response digests.  Two contracts are supported:

* ``targeted``: fetch exactly the explicitly authorised card IDs (capped by the
  detail budget); no probability weights and no whole-page inference.
* ``probability``: stratify by a list-visible field (company), give every
  stratum a positive allocation when the budget allows it, then sample without
  replacement inside each stratum.  Inclusion probabilities, the allocation
  rule, the seed, and the frame digest are all recorded.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import ConfigError
from .fingerprint import canonical_json, sha256_text
from .models import ListItem

CONTRACTS = ("targeted", "probability")
STRATUM_FIELD = "company"
POSITIVE_VALUE = "实习"


def _number(value: float | None) -> float | None:
    return None if value is None else round(float(value), 10)


@dataclass(frozen=True)
class SamplingRequest:
    contract: str
    target_ids: tuple[str, ...] = ()
    seed: int | None = None

    def __post_init__(self) -> None:
        if self.contract not in CONTRACTS:
            raise ConfigError(f"unknown sampling contract: {self.contract!r}")
        if self.contract == "targeted" and not self.target_ids:
            raise ConfigError("targeted sampling requires explicit target IDs")
        if self.contract == "probability" and self.seed is None:
            raise ConfigError("probability sampling requires an explicit integer seed")


@dataclass(frozen=True)
class SamplingFrame:
    run_id: str
    digest: str
    cards: tuple[ListItem, ...]
    list_documents: tuple[tuple[str, str], ...]
    list_cards: int
    duplicate_ids: tuple[str, ...]
    duplicate_count: int
    stratum_field: str = STRATUM_FIELD

    def ids(self) -> tuple[str, ...]:
        return tuple(card.external_id for card in self.cards)

    def by_id(self) -> dict[str, ListItem]:
        return {card.external_id: card for card in self.cards}

    def stratum(self, card: ListItem) -> str:
        return str(getattr(card, self.stratum_field, "") or "")

    def strata(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for card in self.cards:
            grouped.setdefault(self.stratum(card), []).append(card.external_id)
        return {name: tuple(ids) for name, ids in grouped.items()}


def build_frame(
    run_id: str,
    items: Sequence[ListItem],
    *,
    list_documents: Sequence[tuple[str, str]] = (),
    list_cards: int | None = None,
    duplicate_ids: Sequence[str] = (),
) -> SamplingFrame:
    """Freeze the de-duplicated card set and bind it to list response digests."""
    unique: dict[str, ListItem] = {}
    duplicates = set(duplicate_ids)
    for item in items:
        if item.external_id in unique:
            duplicates.add(item.external_id)
            continue
        unique[item.external_id] = item
    cards = tuple(unique.values())
    extra = len(items) - len(unique)
    documents = tuple((str(url), str(digest)) for url, digest in list_documents)
    payload = {
        "stratum_field": STRATUM_FIELD,
        "list_cards": int(list_cards if list_cards is not None else len(items)),
        "list_document_digests": [digest for _, digest in documents],
        "cards": [
            [card.external_id, card.title, card.company, card.location, card.salary]
            for card in cards
        ],
        "duplicate_ids": sorted(set(duplicates)),
    }
    return SamplingFrame(
        run_id=run_id, digest=sha256_text(canonical_json(payload)), cards=cards,
        list_documents=documents,
        list_cards=int(list_cards if list_cards is not None else len(items)),
        duplicate_ids=tuple(sorted(duplicates)), duplicate_count=extra,
    )


@dataclass(frozen=True)
class SelectedCard:
    external_id: str
    stratum: str
    stratum_size: int
    allocation: int
    inclusion_probability: float | None
    order: int


@dataclass(frozen=True)
class SamplingSelection:
    contract: str
    run_id: str
    frame_digest: str
    seed: int | None
    budget: int
    frame_ids: tuple[str, ...]
    cards: tuple[SelectedCard, ...]
    allocations: dict[str, int]
    stratum_sizes: dict[str, int]
    allocation_rule: str
    duplicate_ids: tuple[str, ...] = ()
    duplicate_count: int = 0
    unresolved_target_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(card.external_id for card in self.cards)

    @property
    def uncovered_ids(self) -> tuple[str, ...]:
        selected = set(self.selected_ids)
        return tuple(card_id for card_id in self.frame_ids if card_id not in selected)

    @property
    def covers_every_card(self) -> bool:
        """True when the selection budget is at least the frame size."""
        return self.budget >= len(self.frame_ids)

    @property
    def uncovered_strata(self) -> dict[str, int]:
        return {name: self.stratum_sizes[name]
                for name, count in self.allocations.items() if count == 0}

    @property
    def all_strata_positive(self) -> bool:
        return (self.contract == "probability" and bool(self.allocations)
                and all(count > 0 for count in self.allocations.values()))

    def covers_frame(self) -> bool:
        return self.covers_every_card and set(self.selected_ids) == set(self.frame_ids)


def _allocation(sizes: dict[str, int], budget: int) -> tuple[dict[str, int], str]:
    order = sorted(sizes, key=lambda name: (-sizes[name], name))
    effective = max(0, min(int(budget), sum(sizes.values())))
    allocations = {name: 0 for name in sizes}
    if effective < len(sizes):
        for name in order[:effective]:
            allocations[name] = 1
        rule = ("budget below stratum count: one to each stratum in descending "
                "size order (ties by name); remaining strata have zero probability")
        return allocations, rule
    for name in sizes:
        allocations[name] = 1
    remaining = effective - len(sizes)
    while remaining > 0:
        candidates = [name for name in order if allocations[name] < sizes[name]]
        if not candidates:
            break
        chosen = min(candidates, key=lambda name: (-(sizes[name] - allocations[name]), name))
        allocations[chosen] += 1
        remaining -= 1
    rule = ("one per stratum, then to the stratum with the largest remaining "
            "capacity (ties by name), capped at stratum size")
    return allocations, rule


def _selection(
    frame: SamplingFrame, chosen: dict[str, list[str]], allocations: dict[str, int], *,
    contract: str, seed: int | None, budget: int, rule: str,
    unresolved: Sequence[str] = (), warnings: Sequence[str] = (),
    order: Sequence[str] | None = None,
) -> SamplingSelection:
    sizes = {name: len(ids) for name, ids in frame.strata().items()}
    wanted = {card_id for ids in chosen.values() for card_id in ids}
    by_id = frame.by_id()
    sequence = [by_id[card_id] for card_id in order if card_id in wanted] if order else [
        card for card in frame.cards if card.external_id in wanted]
    cards: list[SelectedCard] = []
    for card in sequence:
        name = frame.stratum(card)
        probability = (allocations.get(name, 0) / sizes[name]
                       if contract == "probability" and sizes[name] else None)
        cards.append(SelectedCard(
            external_id=card.external_id, stratum=name, stratum_size=sizes[name],
            allocation=allocations.get(name, 0), inclusion_probability=probability,
            order=len(cards),
        ))
    return SamplingSelection(
        contract=contract, run_id=frame.run_id, frame_digest=frame.digest,
        seed=seed if contract == "probability" else None, budget=int(budget),
        frame_ids=frame.ids(), cards=tuple(cards), allocations=dict(allocations),
        stratum_sizes=sizes, allocation_rule=rule, duplicate_ids=frame.duplicate_ids,
        duplicate_count=frame.duplicate_count, unresolved_target_ids=tuple(unresolved),
        warnings=tuple(warnings),
    )


def plan_targeted(frame: SamplingFrame, target_ids: Sequence[str],
                  budget: int) -> SamplingSelection:
    known = frame.by_id()
    ordered: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for target in target_ids:
        target = str(target)
        if target in seen:
            continue
        seen.add(target)
        (ordered if target in known else unresolved).append(target)
    effective = max(0, min(int(budget), len(ordered)))
    selected_ids, truncated = ordered[:effective], ordered[effective:]
    strata = frame.strata()
    allocations = {name: 0 for name in strata}
    chosen: dict[str, list[str]] = {name: [] for name in strata}
    for card_id in selected_ids:
        name = frame.stratum(known[card_id])
        allocations[name] += 1
        chosen[name].append(card_id)
    warnings = []
    if unresolved:
        warnings.append("targeted IDs absent from the current list frame: "
                        + ", ".join(unresolved))
    if truncated:
        warnings.append("targeted IDs deferred by detail budget: " + ", ".join(truncated))
    return _selection(
        frame, chosen, allocations, contract="targeted", seed=None, budget=budget,
        rule="explicit target IDs in authorisation order, capped by the detail budget",
        unresolved=unresolved, warnings=warnings, order=selected_ids,
    )


def plan_probability(frame: SamplingFrame, budget: int, seed: int) -> SamplingSelection:
    strata = frame.strata()
    sizes = {name: len(ids) for name, ids in strata.items()}
    allocations, rule = _allocation(sizes, budget)
    chosen: dict[str, list[str]] = {}
    for name, ids in strata.items():
        count = allocations[name]
        chosen[name] = [] if count <= 0 else random.Random(
            f"{int(seed)}:{frame.digest}:{name}").sample(sorted(ids), count)
    zero = sorted(name for name, count in allocations.items() if count == 0)
    warnings = (["no positive inclusion probability for strata: " + ", ".join(zero)]
                if zero else [])
    return _selection(
        frame, chosen, allocations, contract="probability", seed=int(seed),
        budget=budget, rule=rule, warnings=warnings,
    )


def default_request() -> SamplingRequest:
    """Fallback when no contract is configured: a reproducible seeded probability plan."""
    return SamplingRequest("probability", seed=0)
