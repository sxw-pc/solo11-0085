"""Collection orchestration: crawl, persist raw evidence, apply lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .clock import Clock, clock_from_config, iso_z, timestamp_slug
from .config import LoadedConfig
from .crawler import CrawlResult, Crawler, failure_payload
from .errors import StorageError
from .http_client import HttpClient
from .lifecycle import LifecycleEngine, LifecycleResult
from .rawstore import RawStore
from .store import Store

_SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass
class CollectionOutcome:
    run_id: str
    scenario: str
    result: CrawlResult
    lifecycle: LifecycleResult
    run: dict[str, Any] | None

    def summary(self) -> dict[str, Any]:
        payload = self.result.summary()
        payload["scenario"] = self.scenario
        payload["lifecycle"] = self.lifecycle.summary()
        return payload


def choose_run_id(
    clock: Clock,
    *,
    scenario: str,
    config_hash: str,
    explicit: str | None = None,
) -> str:
    if explicit:
        return explicit
    stamp = timestamp_slug(clock.now())
    safe_scenario = _SAFE_ID.sub("-", scenario)
    return f"run-{stamp}-{safe_scenario}-{config_hash[:8]}"


def run_collection(
    loaded: LoadedConfig,
    *,
    scenario: str | None = None,
    run_id: str | None = None,
    resume: bool = False,
    clock: Clock | None = None,
    sleep: Any = None,
    business_date: str | None = None,
) -> CollectionOutcome:
    config = loaded.config
    selected_scenario = scenario or config.site.scenario
    active_clock = clock or clock_from_config(config.clock.fixed_at)
    identifier = choose_run_id(active_clock, scenario=selected_scenario,
                               config_hash=loaded.fingerprint, explicit=run_id)
    started_at = iso_z(active_clock.now())
    store = Store(config.storage.database)
    raw_store = RawStore(config.storage.raw_dir, active_clock)
    kwargs = {"sleep": sleep} if sleep is not None else {}
    client = HttpClient(config.crawl, config.site.allowed_hosts,
                        config.site.allowed_ports, active_clock, **kwargs)
    try:
        store.begin_run(run_id=identifier, site_key=config.site.name,
                        scenario=selected_scenario, base_url=config.site.base_url,
                        config_hash=loaded.fingerprint, started_at=started_at,
                        resumed=resume, business_date=business_date or started_at[:10])
        crawler = Crawler(loaded, client, raw_store, active_clock)
        try:
            result = crawler.crawl(identifier, resume=resume)
        except Exception as exc:
            finished = iso_z(active_clock.now())
            notes = {"error_type": type(exc).__name__, "error": str(exc)}
            if store.run_is_finished(identifier):
                notes["replay_of_finished_run"] = True
            try:
                store.finish_run(identifier, status="failed", is_complete=False,
                                 finished_at=finished, stop_reason="exception",
                                 notes=notes, resumed=resume)
            except StorageError:
                pass
            raise
        for document in result.documents:
            store.record_raw_document(document)
        lifecycle = LifecycleEngine(store).apply(
            run_id=identifier, site_key=config.site.name,
            observations=result.observations, complete=result.complete,
            now=iso_z(active_clock.now()),
            close_after_missing_runs=config.lifecycle.close_after_missing_runs,
        )
        notes = {
            "failures": failure_payload(result)[:50], "warnings": result.warnings[:100],
            "selector_misses": result.selector_misses,
            "request_stats": result.request_stats, "lifecycle": lifecycle.summary(),
        }
        store.finish_run(
            identifier, status="complete" if result.complete else "incomplete",
            is_complete=result.complete, finished_at=result.finished_at,
            pages_fetched=result.pages_fetched, list_cards=result.list_cards,
            unique_jobs=result.unique_jobs, details_ok=result.details_ok,
            details_failed=result.details_failed, duplicate_ids=result.duplicate_ids,
            parse_warnings=result.parse_warnings, stop_reason=result.stop_reason,
            notes=notes, resumed=result.resumed,
        )
        return CollectionOutcome(run_id=identifier, scenario=selected_scenario,
                                 result=result, lifecycle=lifecycle,
                                 run=store.get_run(identifier))
    finally:
        client.close()
        store.close()

