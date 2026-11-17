"""Per-detail fetch/parse isolation shared by the crawler."""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock
from .errors import FetchError
from .http_client import HttpClient, HttpResponse
from .models import JobObservation, ListItem
from .parser import parse_detail_page
from .rawstore import RawDocument, RawStore
from .selectors import Selectors


@dataclass(frozen=True)
class DetailFailure:
    external_id: str
    url: str
    error: str
    status: int | None = None
    retryable: bool = False


@dataclass(frozen=True)
class DetailOutcome:
    selector_misses: dict[str, int]
    warnings: list[str]
    observation: JobObservation | None
    failure: DetailFailure | None


class DetailFetcher:
    def __init__(self, client: HttpClient, raw_store: RawStore, selectors: Selectors,
                 site_key: str, clock: Clock):
        self.client = client
        self.raw_store = raw_store
        self.selectors = selectors
        self.site_key = site_key
        self.clock = clock

    def fetch(self, item: ListItem, run_id: str,
              documents: list[RawDocument]) -> DetailOutcome:
        try:
            response: HttpResponse = self.client.get(item.detail_url)
            documents.append(self.raw_store.put(
                response.content, url=response.url, kind="detail", run_id=run_id,
                http_status=response.status, content_type=response.content_type,
                fetched_at=response.fetched_at,
            ))
            detail = parse_detail_page(
                response.text, response.url, self.site_key, item, self.selectors
            )
            warnings = [f"{item.external_id}: {issue}" for issue in detail.issues]
            if detail.observation is None:
                return DetailOutcome(
                    detail.selector_misses, warnings, None,
                    DetailFailure(
                        external_id=item.external_id, url=item.detail_url,
                        error="required field(s) missing: "
                              + ", ".join(detail.required_missing),
                    ),
                )
            return DetailOutcome(detail.selector_misses, warnings, detail.observation, None)
        except FetchError as exc:
            return DetailOutcome({}, [], None, DetailFailure(
                external_id=item.external_id, url=item.detail_url, error=exc.message,
                status=exc.status, retryable=exc.retryable,
            ))
        except Exception as exc:  # noqa: BLE001 - isolate parser/storage defects per detail.
            return DetailOutcome({}, [], None, DetailFailure(
                external_id=item.external_id, url=item.detail_url,
                error=f"unexpected detail error: {exc}",
            ))
