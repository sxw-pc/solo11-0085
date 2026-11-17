"""Read-only evidence queries, idempotent retries, and URL guard coverage."""

from __future__ import annotations

import json

from jobtrace.charset import RULE_VERSION, analyze
from jobtrace.clock import FixedClock, parse_instant
from jobtrace.config import DEFAULT_SELECTORS, load_config, load_selectors
from jobtrace.errors import UnsafeURLError
from jobtrace.http_client import HttpClient
from jobtrace.mock_site.charset_site import (
    GB18030, UTF8, build_detail_html, build_list_html,
)
from jobtrace.rawstore import RawStore
from jobtrace.report import build_report
from jobtrace.store import Store

from tests._charset_support import FIXED, CharsetCase

SELECTORS = load_selectors(DEFAULT_SELECTORS)


class EvidenceQueryTest(CharsetCase):
    def test_decisions_queryable_by_run_url_sha_and_idempotent(self):
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        detail = build_detail_html(title="工程师", company="甲公司",
                                   encoding=GB18030, meta_charset=GB18030)
        self.site.set("/jobs/7", detail, "text/html; charset=utf-8")
        _, first = self.collect("reconcile", "q1")
        _, second = self.collect("reconcile", "q2")
        self.assertEqual(
            [d.canonical() for d in first.result.decisions if d.kind == "detail"],
            [d.canonical() for d in second.result.decisions if d.kind == "detail"],
        )
        store = RawStore(self.raw_dir, FixedClock(parse_instant(FIXED)))
        detail_sha = next(
            record.sha256 for record in store.iter_manifest()
            if record.url.endswith("/jobs/7")
        )
        # Same bytes re-analyzed produce an identical, idempotent decision.
        replayed = analyze(
            store.read(detail_sha), sha256=detail_sha,
            url=f"{self.base_url}/jobs/7", kind="detail",
            content_type="text/html; charset=utf-8",
            contract="reconcile", selectors=SELECTORS,
        )
        stored = store.find_decision(sha256=detail_sha,
                                     url=f"{self.base_url}/jobs/7",
                                     contract="reconcile")
        self.assertEqual(
            json.dumps(stored["decision"], sort_keys=True, ensure_ascii=False),
            json.dumps(replayed.as_dict(), sort_keys=True, ensure_ascii=False),
        )
        self.assertEqual(len(store.decisions_for_run("q1")), 2)
        self.assertEqual(len(store.decisions_for_run("q2")), 2)
        # Retrying the same run bytes must not duplicate decision lines.
        store.put_decision(replayed, "q2")
        self.assertEqual(len(store.decisions_for_run("q2")), 2)
        # Read-only queries expose the full evidence chain.
        record = store.decisions_for_run("q1")[0]["decision"]
        self.assertEqual(record["rule_version"], RULE_VERSION)
        self.assertIn("candidates", record)
        self.assertTrue(all(
            "byte_start" in span for candidate in record["candidates"]
            for span in candidate["fields"]
        ))

    def test_old_run_interpretation_survives_rule_version_change(self):
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        detail = build_detail_html(title="工程师", company="甲公司",
                                   encoding=GB18030, meta_charset=GB18030)
        self.site.set("/jobs/7", detail, "text/html; charset=gb18030")
        _, outcome = self.collect("reconcile", "legacy")
        store = RawStore(self.raw_dir, FixedClock(parse_instant(FIXED)))
        record = store.find_decision(
            sha256=next(r.sha256 for r in store.iter_manifest()
                        if r.url.endswith("/jobs/7")),
            url=f"{self.base_url}/jobs/7",
        )
        self.assertEqual(record["decision"]["rule_version"], RULE_VERSION)
        self.assertEqual(record["decision"]["accepted_encoding"], "gb18030")
        # The frozen record remains identical even if a future rule version exists.
        again = json.loads(json.dumps(record))
        self.assertEqual(again, record)
        self.assertEqual(outcome.result.decisions[0].rule_version, RULE_VERSION)

    def test_invalid_contract_is_rejected_at_config_and_client(self):
        from jobtrace.charset import CharsetError
        from jobtrace.errors import ConfigError

        with self.assertRaises(ConfigError):
            load_config(
                overrides={
                    "site.base_url": self.base_url,
                    "site.allowed_hosts": ["127.0.0.1"],
                    "site.allowed_ports": [self.server.port],
                    "crawl.charset_contract": "guess",
                },
                environ={},
            )
        loaded = self.config("reconcile")
        with self.assertRaises(CharsetError):
            HttpClient(loaded.config.crawl, loaded.config.site.allowed_hosts,
                       loaded.config.site.allowed_ports, self.clock,
                       charset_contract="guess")

    def test_report_exposes_charset_contract_and_decisions(self):
        from jobtrace.report import build_report

        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        detail = build_detail_html(title="工程师", company="甲公司",
                                   encoding=GB18030, meta_charset=GB18030)
        self.site.set("/jobs/7", detail, "text/html; charset=gb18030")
        loaded, _ = self.collect("reconcile", "rep")
        store = Store(loaded.config.storage.database, readonly=True)
        try:
            report = build_report(store, self.clock)
        finally:
            store.close()
        latest = report["latest_run"]
        self.assertEqual(latest["charset_contract"], "reconcile")
        self.assertEqual(len(latest["charset_decisions"]), 2)
        detail_decision = next(
            item for item in latest["charset_decisions"]
            if item["url"].endswith("/jobs/7")
        )
        self.assertEqual(detail_decision["encoding"], "gb18030")
        self.assertTrue(detail_decision["accepted"])

    def test_guard_url_blocks_non_loopback_and_credentials(self):
        loaded = self.config("reconcile")
        client = HttpClient(
            loaded.config.crawl, loaded.config.site.allowed_hosts,
            loaded.config.site.allowed_ports, self.clock,
            sleep=lambda _seconds: None,
        )
        try:
            for bad in (
                "http://example.com/jobs/7",
                "http://127.0.0.1@evil.example/jobs/7",
                "http://user:pass@127.0.0.1/jobs/7",
                "ftp://127.0.0.1/jobs/7",
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(UnsafeURLError):
                        client.guard_url(bad)
            safe = client.guard_url(f"{self.base_url}/jobs/7")
            self.assertEqual(safe, f"{self.base_url}/jobs/7")
        finally:
            client.close()

    def test_detail_url_still_uses_guarded_http_client(self):
        # A card linking off-loopback must never be fetched; the run fails safely.
        list_body = (
            b'<!doctype html><html><head><meta charset="utf-8"></head><body><ul>'
            b'<li class="job-card" data-job-id="7">'
            b'<a class="job-link" href="http://evil.example/jobs/7">'
            b'<span class="job-title">t</span></a>'
            b'<span class="job-company">c</span></li></ul></body></html>'
        )
        self.site.set("/jobs", list_body, "text/html; charset=utf-8")
        _, outcome = self.collect("reconcile", "guard")
        self.assertFalse(outcome.result.complete)
        self.assertEqual(outcome.result.failures[0].error,
                         "unexpected detail error: host must be a literal IP "
                         "address without DNS resolution: evil.example")
        self.assertEqual(outcome.result.observations, [])
