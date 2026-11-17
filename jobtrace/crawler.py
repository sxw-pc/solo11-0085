"""Incremental list/detail crawl with isolation, guards, and checkpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

from .clock import Clock, iso_z
from .config import LoadedConfig
from .detail_worker import DetailFailure, DetailFetcher
from .errors import PaginationLimitError, PaginationLoopError
from .http_client import HttpClient, HttpResponse
from .models import JobObservation, ListItem
from .page_contracts import verdict_for
from .page_epoch import EvidenceLedger
from .pagination import Cursor, PaginationGuard
from .parser import merge_selector_misses, page_observation, parse_list_page
from .rawstore import RawDocument, RawStore
from .resume import revalidate_prefix


@dataclass
class CrawlResult:
    run_id: str
    site_key: str
    started_at: str
    finished_at: str
    pages_fetched: int
    list_cards: int
    unique_jobs: int
    details_ok: int
    details_failed: int
    duplicate_ids: int
    parse_warnings: int
    observations: list[JobObservation] = field(default_factory=list)
    failures: list[DetailFailure] = field(default_factory=list)
    documents: list[RawDocument] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selector_misses: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "natural_end"
    complete: bool = True
    resumed: bool = False
    request_stats: dict[str, Any] = field(default_factory=dict)
    contract: str = "fixed_snapshot"
    list_verdict: dict[str, Any] = field(default_factory=dict)
    screen_complete: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "site_key": self.site_key,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "pages_fetched": self.pages_fetched, "list_cards": self.list_cards,
            "unique_jobs": self.unique_jobs, "details_ok": self.details_ok,
            "details_failed": self.details_failed, "duplicate_ids": self.duplicate_ids,
            "parse_warnings": self.parse_warnings, "stop_reason": self.stop_reason,
            "complete": self.complete, "resumed": self.resumed,
            "request_stats": self.request_stats, "contract": self.contract,
            "list_verdict": self.list_verdict,
        }


class Crawler:
    def __init__(self, loaded: LoadedConfig, client: HttpClient,
                 raw_store: RawStore, clock: Clock, *,
                 contract: str = "fixed_snapshot"):
        self.loaded = loaded
        self.site = loaded.config.site
        self.selectors = loaded.selectors
        self.client = client
        self.raw_store = raw_store
        self.clock = clock
        self.contract = contract

    def _first_url(self) -> str:
        separator = "&" if "?" in self.site.list_path else "?"
        return f"{self.site.base_url}{self.site.list_path}{separator}{urlencode({self.site.page_param: 1})}"

    def _document(self, response: HttpResponse, kind: str, run_id: str) -> RawDocument:
        return self.raw_store.put(
            response.content, url=response.url, kind=kind, run_id=run_id,
            http_status=response.status, content_type=response.content_type,
            fetched_at=response.fetched_at,
        )

    def _save_cursor(self, guard: PaginationGuard, next_url: str | None, completed: bool,
                     ledger: EvidenceLedger, snapshot_id: str | None) -> None:
        cursor = guard.cursor(
            site_key=self.site.name, next_url=next_url,
            updated_at=iso_z(self.clock.now()), completed=completed,
            snapshot_id=snapshot_id, contract=self.contract,
            pages=[item.for_cursor() for item in ledger.accepted_observations()],
        )
        cursor.save(self.loaded.config.storage.checkpoint)

    def _resume_cursor(self) -> Cursor | None:
        cursor = Cursor.load(self.loaded.config.storage.checkpoint)
        if cursor is None or cursor.site_key != self.site.name:
            return None
        if cursor.completed or not cursor.next_url:
            return None
        return cursor

    def _replay_prefix(self, ledger: EvidenceLedger, cursor: Cursor, run_id: str,
                       documents: list[RawDocument]) -> tuple[bool, str]:
        """Fetch and compare every stored page before trusting ``cursor.next_url``."""

        def archive(response: HttpResponse) -> None:
            documents.append(self._document(response, "list", run_id))

        return revalidate_prefix(
            self.client, ledger, cursor, self.selectors, self.site.page_param,
            archive, self._first_url(),
        )

    def crawl(self, run_id: str, *, resume: bool = False) -> CrawlResult:
        started_at = iso_z(self.clock.now())
        guard = PaginationGuard(max_pages=self.site.max_pages)
        ledger = EvidenceLedger(self.site.page_param)
        start_url, visited, resumed, resumed_ok = self._first_url(), [], False, False
        expected_snapshot: str | None = None
        if resume:
            cursor = self._resume_cursor()
            if cursor is not None:
                if cursor.contract in (None, self.contract):
                    start_url = cursor.next_url or start_url
                    visited, resumed = cursor.visited_pages, True
                    expected_snapshot = cursor.snapshot_id
                else:
                    resumed, start_url = True, self._first_url()
        try:
            guard.start(start_url, visited)
        except PaginationLimitError:
            guard = PaginationGuard(max_pages=self.site.max_pages)
            start_url, resumed, expected_snapshot = self._first_url(), False, None
            guard.start(start_url)

        observations: list[JobObservation] = []
        failures: list[DetailFailure] = []
        documents: list[RawDocument] = []
        warnings: list[str] = []
        selector_misses: dict[str, int] = {}
        discovered: dict[str, ListItem] = {}
        duplicates = list_cards = pages_fetched = 0
        fatal, stop_reason = False, "natural_end"
        current_url: str | None = start_url
        prefix_checked = not resumed or cursor is None or not cursor.pages
        if not prefix_checked:
            resumed_ok, start_url = self._replay_prefix(ledger, cursor, run_id, documents)
            prefix_checked = True
            current_url = start_url
            if not resumed_ok:
                warnings.append("resume prefix diverged; starting a new list segment")
                guard = PaginationGuard(max_pages=self.site.max_pages)
                guard.start(start_url)
        while current_url:
            response = self.client.get(current_url)
            pages_fetched += 1
            documents.append(self._document(response, "list", run_id))
            parsed = parse_list_page(response.text, response.url, self.selectors)
            list_cards += parsed.card_count
            warnings.extend(parsed.issues)
            selector_misses = merge_selector_misses(selector_misses, parsed.selector_misses)
            ledger.observe(page_observation(response, current_url, parsed))
            for item in parsed.items:
                if item.external_id in discovered:
                    duplicates += 1
                    warnings.append(f"duplicate job id on listing: {item.external_id}")
                    continue
                discovered[item.external_id] = item
            try:
                next_url = guard.advance(parsed.next_url)
            except PaginationLoopError as exc:
                warnings.append(str(exc))
                stop_reason, fatal, next_url = "pagination_loop", True, None
            except PaginationLimitError as exc:
                warnings.append(str(exc))
                stop_reason, fatal, next_url = "pagination_limit", True, None
            self._save_cursor(guard, next_url, completed=next_url is None,
                              ledger=ledger, snapshot_id=parsed.declaration.snapshot_id)
            current_url = next_url
            if fatal:
                break

        verdict = verdict_for(
            self.contract, ledger, expected_snapshot=expected_snapshot,
        )
        if resumed and expected_snapshot and verdict.snapshot_id \
                and set(ledger.by_epoch) != {expected_snapshot}:
            warnings.append(
                "resume could not continue the cursor's snapshot; evidence is segmented")
        warnings.extend(item["detail"] for item in verdict.witnesses)
        items = self._select_items(discovered, ledger, verdict, warnings)
        if len(items) > self.site.max_details:
            warnings.append(
                f"detail limit reached: discovered {len(items)}, fetching {self.site.max_details}"
            )
            items, fatal, stop_reason = items[: self.site.max_details], True, "detail_limit"
        for item in items:
            outcome = DetailFetcher(
                self.client, self.raw_store, self.selectors, self.site.name, self.clock
            ).fetch(item, run_id, documents)
            selector_misses = merge_selector_misses(selector_misses,
                                                    outcome.selector_misses)
            warnings.extend(outcome.warnings)
            if outcome.observation is not None:
                observations.append(outcome.observation)
            elif outcome.failure is not None:
                failures.append(outcome.failure)
                fatal = True

        screen_complete = not fatal
        if not screen_complete and stop_reason == "natural_end":
            stop_reason = "incomplete"
        # The live-observation contract never lets a partial view advance the
        # lifecycle: only a proven, whole fixed-snapshot chain can be "complete".
        lifecycle_eligible = verdict.claims_all_jobs
        if not lifecycle_eligible:
            fatal = True
            if stop_reason == "natural_end":
                if resumed:
                    stop_reason = "resumed_partial"
                elif self.contract == "live_observation":
                    stop_reason = "live_observation"
                else:
                    stop_reason = verdict.stop_reason
        if failures:
            fatal = True
            if stop_reason == "natural_end":
                stop_reason = "detail_failures"
        if resumed:
            # A resumed round never observes the whole snapshot in one pass as a
            # first-class run does; it must stay partial even when the replayed
            # prefix re-validates, so it can never close jobs it did not re-see.
            fatal = True
            if stop_reason == "natural_end":
                stop_reason = "resumed_partial"
        if not discovered:
            fatal, stop_reason = True, "no_jobs"
        if fatal and stop_reason == "natural_end":
            stop_reason = "incomplete"
        elif verdict.complete and stop_reason == "natural_end":
            stop_reason = verdict.stop_reason
        return CrawlResult(
            run_id=run_id, site_key=self.site.name, started_at=started_at,
            finished_at=iso_z(self.clock.now()), pages_fetched=pages_fetched,
            list_cards=list_cards, unique_jobs=len(discovered),
            details_ok=len(observations), details_failed=len(failures),
            duplicate_ids=duplicates, parse_warnings=len(warnings),
            observations=observations, failures=failures, documents=documents,
            warnings=warnings, selector_misses=dict(sorted(selector_misses.items())),
            stop_reason=stop_reason, complete=not fatal, resumed=resumed,
            request_stats=self.client.stats.as_dict(), contract=self.contract,
            list_verdict=verdict.as_dict(), screen_complete=screen_complete,
        )

    def _select_items(self, discovered: dict[str, ListItem], ledger: EvidenceLedger,
                      verdict: Any, warnings: list[str]) -> list[ListItem]:
        """Only a proven complete snapshot may feed the lifecycle ledger."""

        if self.contract == "fixed_snapshot" and not verdict.complete:
            warnings.append("fixed snapshot contract: no complete chain; "
                            "published jobs and events remain unchanged")
            return []
        if self.contract == "live_observation" and not verdict.claims_all_jobs:
            trusted: dict[str, None] = {}
            for segment in verdict.segments:
                for start, end in segment["coverage_ranges"]:
                    for item in segment["range_items"][f"{start}-{end}"]:
                        trusted.setdefault(item, None)
            if set(trusted) != set(discovered):
                warnings.append("live observations cover only part of the listing")
            return [discovered[item] for item in trusted if item in discovered]
        return [discovered[item] for item in verdict.items if item in discovered]


def failure_payload(result: CrawlResult) -> list[dict[str, Any]]:
    return [asdict(failure) for failure in result.failures]
