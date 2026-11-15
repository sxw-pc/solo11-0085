"""Incremental list/detail crawl with isolation, guards, and checkpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

from .clock import Clock, iso_z
from .config import LoadedConfig
from .errors import FetchError, PaginationLimitError, PaginationLoopError
from .http_client import HttpClient, HttpResponse
from .models import JobObservation, ListItem
from .pagination import Cursor, PaginationGuard
from .parser import merge_selector_misses, parse_detail_page, parse_list_page
from .rawstore import RawDocument, RawStore


@dataclass(frozen=True)
class DetailFailure:
    external_id: str
    url: str
    error: str
    status: int | None = None
    retryable: bool = False


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

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "site_key": self.site_key,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "pages_fetched": self.pages_fetched, "list_cards": self.list_cards,
            "unique_jobs": self.unique_jobs, "details_ok": self.details_ok,
            "details_failed": self.details_failed, "duplicate_ids": self.duplicate_ids,
            "parse_warnings": self.parse_warnings, "stop_reason": self.stop_reason,
            "complete": self.complete, "resumed": self.resumed,
            "request_stats": self.request_stats,
        }


class Crawler:
    def __init__(self, loaded: LoadedConfig, client: HttpClient,
                 raw_store: RawStore, clock: Clock):
        self.loaded = loaded
        self.site = loaded.config.site
        self.selectors = loaded.selectors
        self.client = client
        self.raw_store = raw_store
        self.clock = clock

    def _first_url(self) -> str:
        separator = "&" if "?" in self.site.list_path else "?"
        return f"{self.site.base_url}{self.site.list_path}{separator}{urlencode({self.site.page_param: 1})}"

    def _document(self, response: HttpResponse, kind: str, run_id: str) -> RawDocument:
        return self.raw_store.put(
            response.content, url=response.url, kind=kind, run_id=run_id,
            http_status=response.status, content_type=response.content_type,
            fetched_at=response.fetched_at,
        )

    def _save_cursor(self, guard: PaginationGuard, next_url: str | None, completed: bool) -> None:
        cursor = guard.cursor(
            site_key=self.site.name, next_url=next_url,
            updated_at=iso_z(self.clock.now()), completed=completed,
        )
        cursor.save(self.loaded.config.storage.checkpoint)

    def _resume_cursor(self) -> Cursor | None:
        cursor = Cursor.load(self.loaded.config.storage.checkpoint)
        if cursor is None or cursor.site_key != self.site.name:
            return None
        if cursor.completed or not cursor.next_url:
            return None
        return cursor

    def crawl(self, run_id: str, *, resume: bool = False) -> CrawlResult:
        started_at = iso_z(self.clock.now())
        guard = PaginationGuard(max_pages=self.site.max_pages)
        start_url, visited, resumed = self._first_url(), [], False
        if resume:
            cursor = self._resume_cursor()
            if cursor is not None:
                start_url = cursor.next_url or start_url
                visited, resumed = cursor.visited_pages, True
        try:
            guard.start(start_url, visited)
        except PaginationLimitError:
            guard = PaginationGuard(max_pages=self.site.max_pages)
            start_url, resumed = self._first_url(), False
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
        while current_url:
            response = self.client.get(current_url)
            pages_fetched += 1
            documents.append(self._document(response, "list", run_id))
            parsed = parse_list_page(response.text, response.url, self.selectors)
            list_cards += parsed.card_count
            warnings.extend(parsed.issues)
            selector_misses = merge_selector_misses(selector_misses, parsed.selector_misses)
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
            self._save_cursor(guard, next_url, completed=next_url is None)
            current_url = next_url
            if fatal:
                break

        items = list(discovered.values())
        if len(items) > self.site.max_details:
            warnings.append(
                f"detail limit reached: discovered {len(items)}, fetching {self.site.max_details}"
            )
            items, fatal, stop_reason = items[: self.site.max_details], True, "detail_limit"
        for item in items:
            try:
                response = self.client.get(item.detail_url)
                documents.append(self._document(response, "detail", run_id))
                detail = parse_detail_page(
                    response.text, response.url, self.site.name, item, self.selectors
                )
                selector_misses = merge_selector_misses(selector_misses, detail.selector_misses)
                warnings.extend(f"{item.external_id}: {issue}" for issue in detail.issues)
                if detail.observation is None:
                    fatal = True
                    failures.append(DetailFailure(
                        external_id=item.external_id, url=item.detail_url,
                        error="required field(s) missing: " + ", ".join(detail.required_missing),
                    ))
                    continue
                observations.append(detail.observation)
            except FetchError as exc:
                fatal = True
                failures.append(DetailFailure(
                    external_id=item.external_id, url=item.detail_url, error=exc.message,
                    status=exc.status, retryable=exc.retryable,
                ))
            except Exception as exc:  # Isolate parser/storage defects per detail.
                fatal = True
                failures.append(DetailFailure(
                    external_id=item.external_id, url=item.detail_url,
                    error=f"unexpected detail error: {exc}",
                ))

        if resumed:
            fatal = True
            if stop_reason == "natural_end":
                stop_reason = "resumed_partial"
        if not discovered:
            fatal, stop_reason = True, "no_jobs"
        if not observations and not failures and not fatal:
            fatal, stop_reason = True, "no_details"
        if failures:
            fatal = True
            if stop_reason == "natural_end":
                stop_reason = "detail_failures"
        if fatal and stop_reason == "natural_end":
            stop_reason = "incomplete"
        return CrawlResult(
            run_id=run_id, site_key=self.site.name, started_at=started_at,
            finished_at=iso_z(self.clock.now()), pages_fetched=pages_fetched,
            list_cards=list_cards, unique_jobs=len(discovered),
            details_ok=len(observations), details_failed=len(failures),
            duplicate_ids=duplicates, parse_warnings=len(warnings),
            observations=observations, failures=failures, documents=documents,
            warnings=warnings, selector_misses=dict(sorted(selector_misses.items())),
            stop_reason=stop_reason, complete=not fatal, resumed=resumed,
            request_stats=self.client.stats.as_dict(),
        )


def failure_payload(result: CrawlResult) -> list[dict[str, Any]]:
    return [asdict(failure) for failure in result.failures]
