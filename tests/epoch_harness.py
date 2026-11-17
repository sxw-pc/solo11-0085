"""Shared live-crawler harness for the page-epoch tests (unittest-friendly).

Tests use a real loopback server so a snapshot flip between requests is a genuine
two-response HTTP sequence. The harness is a plain context manager rather than a
pytest fixture so both ``unittest`` and ``pytest`` can drive it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobtrace.clock import FixedClock, parse_instant
from jobtrace.config import load_config
from jobtrace.crawler import Crawler
from jobtrace.http_client import HttpClient
from jobtrace.mock_site.epoch_site import create_epoch_app, default_epoch_site
from jobtrace.rawstore import RawStore
from jobtrace.serving import start_server

FIXED = "2026-01-15T09:00:00Z"


@dataclass
class CrawlEnv:
    loaded: Any
    site: Any
    client: HttpClient
    clock: FixedClock
    base_url: str

    def run(self, contract: str = "fixed_snapshot", *, resume: bool = False,
            run_id: str = "run-epoch", hook: Callable[[str, Any], None] | None = None):
        crawler = Crawler(self.loaded, self.client,
                          RawStore(self.loaded.config.storage.raw_dir, self.clock),
                          self.clock, contract=contract)
        if hook is not None:
            client = self.client

            class _HookedClient:
                stats = client.stats

                def get(self, url: str):
                    response = client.get(url)
                    hook(url, response)
                    return response

            crawler.client = _HookedClient()
        return crawler.crawl(run_id, resume=resume)


@contextmanager
def epoch_environment(tmp_path: str | Path) -> Iterator[CrawlEnv]:
    base = Path(tmp_path)
    site = default_epoch_site()
    server = start_server(create_epoch_app(site), port=0)
    try:
        base_url = f"http://127.0.0.1:{server.port}"
        loaded = load_config(
            overrides={
                "site.base_url": base_url, "site.allowed_hosts": ["127.0.0.1"],
                "site.allowed_ports": [server.port],
                "site.max_pages": 50, "site.max_details": 100,
                "storage.database": str(base / "db.sqlite3"),
                "storage.raw_dir": str(base / "raw"),
                "storage.checkpoint": str(base / "checkpoint.json"),
                "clock.fixed_at": FIXED,
            },
            environ={},
        )
        clock = FixedClock(parse_instant(FIXED))
        client = HttpClient(loaded.config.crawl, loaded.config.site.allowed_hosts,
                            loaded.config.site.allowed_ports, clock,
                            sleep=lambda _seconds: None)
        try:
            yield CrawlEnv(loaded=loaded, site=site, client=client, clock=clock,
                           base_url=base_url)
        finally:
            client.close()
    finally:
        server.stop()
