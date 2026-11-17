"""Snapshot-scoped list-page evidence, chain proofs, and completeness verdicts.

A list "epoch" is one claimed ``snapshot_id``. It is only *proven* complete when
page 1 through the declared last page form a contiguous, gap-free, duplicate-free
index chain whose URLs, response digests, and ``next_url`` relations all agree.
Natural end of pagination is never accepted as a substitute for this proof.

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

from .fingerprint import canonical_json, sha256_text
from .pagination import normalized_page_url, page_number

CONTRACTS = ("fixed_snapshot", "live_observation")


def _canonical_url(url: str) -> str:
    try:
        return normalized_page_url(url)
    except ValueError:
        return url


def items_digest(items: Iterable[Any]) -> str:
    """Digest card payloads rather than their arrival order."""

    return sha256_text(canonical_json([
        [item.external_id, item.title, item.company, item.location, item.salary,
         item.detail_url] for item in items
    ]))


@dataclass(frozen=True)
class PageDeclaration:
    snapshot_id: str | None
    page_index: int | None
    total_pages: int | None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageObservation:
    """One list response bound to its snapshot declaration and byte digest."""

    url: str
    normalized_url: str
    sha256: str
    http_status: int
    declared: PageDeclaration
    items: tuple[str, ...]
    item_digest: str
    next_url: str | None
    prev_url: str | None = None

    @property
    def snapshot_id(self) -> str | None:
        return self.declared.snapshot_id

    @property
    def page_index(self) -> int | None:
        return self.declared.page_index

    @property
    def total_pages(self) -> int | None:
        return self.declared.total_pages

    @property
    def response_digest(self) -> str:
        return sha256_text("\x1f".join((
            self.normalized_url, self.sha256, str(self.http_status),
            str(self.snapshot_id), str(self.page_index), str(self.total_pages),
            self.next_url or "")))

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "normalized_url": self.normalized_url,
            "sha256": self.sha256, "http_status": self.http_status,
            "snapshot_id": self.snapshot_id, "page_index": self.page_index,
            "total_pages": self.total_pages, "next_url": self.next_url,
            "prev_url": self.prev_url, "response_digest": self.response_digest,
            "item_digest": self.item_digest,
        }

    def for_cursor(self) -> dict[str, Any]:
        payload = self.as_dict()
        payload.pop("response_digest")
        payload.update(problems=list(self.declared.problems), items=list(self.items))
        return payload

    @classmethod
    def from_cursor(cls, payload: dict[str, Any]) -> PageObservation:
        def number(value: Any) -> int | None:
            return None if value is None else int(value)

        return cls(
            url=str(payload["url"]), normalized_url=str(payload["normalized_url"]),
            sha256=str(payload["sha256"]), http_status=int(payload["http_status"]),
            declared=PageDeclaration(
                payload.get("snapshot_id"), number(payload.get("page_index")),
                number(payload.get("total_pages")),
                tuple(str(item) for item in payload.get("problems", [])),
            ),
            items=tuple(str(item) for item in payload.get("items", [])),
            item_digest=str(payload.get("item_digest", "")),
            next_url=payload.get("next_url"), prev_url=payload.get("prev_url"),
        )


@dataclass
class Witness:
    """A conflict or missing-page fact kept with its response provenance."""

    kind: str
    observation: PageObservation
    detail: str
    prior: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        item = self.observation
        return {"kind": self.kind, "snapshot_id": item.snapshot_id,
                "page_index": item.page_index, "url": item.url, "sha256": item.sha256,
                "response_digest": item.response_digest, "detail": self.detail,
                "prior": list(self.prior)}


@dataclass
class EpochChain:
    snapshot_id: str
    pages: dict[int, PageObservation] = field(default_factory=dict)
    links: set[int] = field(default_factory=set)
    total_pages: int | None = None
    total_pages_seen: set[int] = field(default_factory=set)
    witnesses: list[Witness] = field(default_factory=list)

    @property
    def indices(self) -> list[int]:
        return sorted(self.pages)

    @property
    def complete(self) -> bool:
        if not self.pages or self.total_pages is None or self.witnesses:
            return False
        if self.total_pages_seen != {self.total_pages}:
            return False
        return (set(self.pages) == set(range(1, self.total_pages + 1))
                and set(range(1, self.total_pages)) <= self.links)

    def missing_pages(self) -> list[int]:
        total = self.total_pages or 0
        return [index for index in range(1, total + 1) if index not in self.pages]

    def coverage_ranges(self) -> list[tuple[int, int]]:
        """Contiguous ranges over this epoch's observed page indices."""

        ranges: list[tuple[int, int]] = []
        for index in self.indices:
            if ranges and ranges[-1][1] == index - 1 and index - 1 in self.links:
                ranges[-1] = (ranges[-1][0], index)
            else:
                ranges.append((index, index))
        return ranges

    def items_for(self, indices: Iterable[int]) -> list[str]:
        seen: dict[str, None] = {}
        for index in indices:
            for item in self.pages[index].items:
                seen.setdefault(item, None)
        return list(seen)

    def as_dict(self) -> dict[str, Any]:
        ranges = self.coverage_ranges()
        return {
            "snapshot_id": self.snapshot_id, "total_pages": self.total_pages,
            "observed_pages": self.indices, "missing_pages": self.missing_pages(),
            "coverage_ranges": [list(s) for s in ranges],
            "range_items": {f"{a}-{b}": self.items_for(range(a, b + 1)) for a, b in ranges},
            "complete": self.complete,
            "witnesses": [item.as_dict() for item in self.witnesses],
        }


class EvidenceLedger:
    """Accumulates observations and rejects silent overwrites.

    Every response for a normalized URL is remembered by digest. Different bytes
    for an already-seen URL raise ``content_changed``; the earlier response is
    kept and never replaced by arrival order.
    """

    def __init__(self, page_param: str):
        self.page_param = page_param
        self.observations: list[PageObservation] = []
        self.by_url: dict[str, list[PageObservation]] = {}
        self.by_epoch: dict[str, EpochChain] = {}
        self.witnesses: list[Witness] = []

    def _witness(self, epoch: EpochChain | None, observation: PageObservation,
                 kind: str, detail: str, prior: list[dict[str, Any]] | None = None) -> None:
        witness = Witness(kind=kind, observation=observation, detail=detail,
                          prior=list(prior or []))
        self.witnesses.append(witness)
        if epoch is not None:
            epoch.witnesses.append(witness)

    def _record(self, observation: PageObservation) -> None:
        previous = self.by_url.setdefault(observation.normalized_url, [])
        if previous and all(item.sha256 != observation.sha256 for item in previous):
            self._witness(None, observation, "content_changed",
                          "same URL changed bytes; earlier responses are kept",
                          prior=[item.as_dict() for item in previous])
        previous.append(observation)
        self.observations.append(observation)

    def observe(self, observation: PageObservation) -> None:
        self._record(observation)
        snapshot, index = observation.snapshot_id, observation.page_index
        if not snapshot or index is None:
            self._witness(None, observation, "declaration_missing",
                          "snapshot_id and page index are required for a chain proof")
            return
        if observation.declared.problems:
            self._witness(None, observation, "declaration_invalid",
                          "; ".join(observation.declared.problems))
            return
        epoch = self.by_epoch.setdefault(snapshot, EpochChain(snapshot_id=snapshot))
        existing = epoch.pages.get(index)
        if existing is not None:
            if existing.sha256 != observation.sha256:
                self._witness(epoch, observation, "duplicate_page",
                              f"page {index} of {snapshot} returned second content; "
                              f"the accepted response is kept",
                              prior=[existing.as_dict()])
            return
        url_index = page_number(observation.url, self.page_param)
        if url_index is not None and url_index != index:
            self._witness(epoch, observation, "chain_conflict",
                          f"URL declares page {url_index} but page declares {index}")
            return
        if observation.total_pages is not None:
            epoch.total_pages_seen.add(observation.total_pages)
            if epoch.total_pages is None:
                epoch.total_pages = observation.total_pages
            elif epoch.total_pages != observation.total_pages:
                self._witness(epoch, observation, "chain_conflict",
                              f"total_pages changed {epoch.total_pages} -> "
                              f"{observation.total_pages}")
        predecessor, successor = epoch.pages.get(index - 1), epoch.pages.get(index + 1)
        backward = successor is None or _canonical_url(
            observation.next_url or "") == successor.normalized_url
        forward = predecessor is None or _canonical_url(
            predecessor.next_url or "") == observation.normalized_url
        if not backward:
            self._witness(epoch, observation, "chain_conflict",
                          f"page {index} next_url {observation.next_url!r} does not lead "
                          f"to page {index + 1} {successor.normalized_url}")
        if not forward:
            self._witness(epoch, observation, "chain_conflict",
                          f"page {index - 1} next_url {predecessor.next_url!r} does not "
                          f"lead to {observation.normalized_url}")
        if observation.total_pages == index and observation.next_url:
            self._witness(epoch, observation, "chain_conflict",
                          f"terminal page {index} still links to {observation.next_url!r}")
        next_index = (page_number(observation.next_url, self.page_param)
                      if observation.next_url else None)
        if next_index is not None and next_index <= index:
            self._witness(epoch, observation, "chain_conflict",
                          f"page {index} next_url points backward to page {next_index}")
            backward = False
        epoch.pages[index] = observation
        if predecessor is not None and forward:
            epoch.links.add(index - 1)
        if successor is not None and backward:
            epoch.links.add(index)

    def complete_epochs(self) -> list[EpochChain]:
        return [epoch for epoch in self.by_epoch.values() if epoch.complete]

    def accepted_observations(self) -> list[PageObservation]:
        """Only pages that were incorporated into an epoch, in acceptance order."""

        accepted = {id(page) for epoch in self.by_epoch.values()
                    for page in epoch.pages.values()}
        return [page for page in self.observations if id(page) in accepted]
