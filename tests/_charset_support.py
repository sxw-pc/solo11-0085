"""Shared unittest helpers: real loopback charset site and isolated runs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jobtrace.clock import FixedClock, parse_instant
from jobtrace.config import load_config
from jobtrace.mock_site.charset_site import CharsetSite, create_charset_app
from jobtrace.pipeline import run_collection
from jobtrace.serving import start_server

FIXED = "2026-01-15T09:00:00Z"
TITLE = "工程师"
COMPANY = "甲公司"


class CharsetCase(unittest.TestCase):
    """Base case wiring a live charset site, configs, and the collector."""

    def setUp(self) -> None:
        self.site = CharsetSite()
        self.server = start_server(create_charset_app(self.site), port=0)
        self.base_url = f"http://127.0.0.1:{self.server.port}"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.raw_dir = root / "raw"
        self.db_path = root / "jobs.sqlite3"
        self.clock = FixedClock(parse_instant(FIXED))

    def tearDown(self) -> None:
        self.server.stop()

    def config(self, contract: str):
        return load_config(
            overrides={
                "site.base_url": self.base_url,
                "site.allowed_hosts": ["127.0.0.1"],
                "site.allowed_ports": [self.server.port],
                "site.max_pages": 2,
                "storage.database": str(self.db_path),
                "storage.raw_dir": str(self.raw_dir),
                "storage.report_dir": str(Path(self.tmp.name) / "reports"),
                "storage.checkpoint": str(Path(self.tmp.name) / "checkpoint.json"),
                "clock.fixed_at": FIXED,
                "crawl.charset_contract": contract,
                "crawl.max_retries": 0,
            },
            environ={},
        )

    def collect(self, contract: str, run_id: str):
        loaded = self.config(contract)
        outcome = run_collection(
            loaded, scenario="charset", run_id=run_id,
            clock=self.clock, sleep=lambda _seconds: None,
        )
        return loaded, outcome
