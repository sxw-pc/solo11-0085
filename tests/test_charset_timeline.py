"""r1/r2 timelines: mislabelled GB18030 detail page under both contracts."""

from __future__ import annotations

import hashlib

from jobtrace.charset import RULE_VERSION
from jobtrace.mock_site.charset_site import (
    COMPANY, GB18030, TITLE, UTF8, build_detail_html, build_list_html,
)
from jobtrace.rawstore import RawStore
from jobtrace.store import Store

from tests._charset_support import CharsetCase

CT_UTF8 = "text/html; charset=utf-8"
CT_GB = "text/html; charset=gb18030"


def _list_page(meta: str):
    return build_list_html(meta_charset=meta), CT_UTF8


def _detail_page(header_encoding: str, *, meta: str | None):
    body = build_detail_html(title=TITLE, company=COMPANY,
                             encoding=GB18030, meta_charset=meta)
    return body, (CT_GB if header_encoding == GB18030 else CT_UTF8)


class TimelineTest(CharsetCase):
    def _serve_r1(self):
        list_body, list_ct = _list_page(UTF8)
        detail_body, detail_ct = _detail_page(GB18030, meta=GB18030)
        self.site.set("/jobs", list_body, list_ct)
        self.site.set("/jobs/7", detail_body, detail_ct)
        return detail_body

    def _serve_r2(self):
        list_body, list_ct = _list_page(UTF8)
        detail_body, detail_ct = _detail_page(UTF8, meta=GB18030)
        self.site.set("/jobs", list_body, list_ct)
        self.site.set("/jobs/7", detail_body, detail_ct)
        return detail_body

    def test_r1_then_r2_reconcile_recovers_and_keeps_stable_fields(self):
        r1_bytes = self._serve_r1()
        _, r1 = self.collect("reconcile", "r1")
        self.assertTrue(r1.result.complete)
        self.assertEqual(r1.result.unique_jobs, 1)
        self.assertEqual(len(r1.result.observations), 1)
        job = r1.result.observations[0]
        self.assertEqual(job.external_id, "7")
        self.assertEqual(job.title, TITLE)
        self.assertEqual(job.company, COMPANY)
        self.assertEqual(job.detail_url, f"{self.base_url}/jobs/7")
        self.assertEqual(r1.lifecycle.new_jobs, 1)
        self.assertEqual(r1.lifecycle.changed_events, 0)
        detail_decision = next(
            item for item in r1.result.decisions if item.kind == "detail"
        )
        self.assertTrue(detail_decision.accepted)
        self.assertEqual(detail_decision.accepted_encoding, "gb18030")
        self.assertEqual(detail_decision.accepted_source, "http")

        r2_bytes = self._serve_r2()
        self.assertEqual(r1_bytes, r2_bytes)
        _, r2 = self.collect("reconcile", "r2")
        self.assertTrue(r2.result.complete)
        decision = next(item for item in r2.result.decisions if item.kind == "detail")
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.ambiguous)
        self.assertEqual(decision.accepted_encoding, "gb18030")
        self.assertEqual(decision.accepted_source, "meta")
        self.assertIn("overrides", decision.reason)
        recovered = r2.result.observations[0]
        self.assertEqual(recovered.title, TITLE)
        self.assertEqual(recovered.company, COMPANY)
        self.assertEqual(r2.lifecycle.changed_events, 0)
        self.assertEqual(r2.lifecycle.new_jobs, 0)

        store = Store(self.db_path)
        try:
            row = store.find_job("jobtrace-demo", "7")
            self.assertIsNotNone(row)
            self.assertEqual(row["title"], TITLE)
            self.assertEqual(row["company"], COMPANY)
        finally:
            store.close()

    def test_r2_strict_contract_fails_page_without_job_changes(self):
        self._serve_r1()
        _, r1 = self.collect("strict", "r1-strict")
        self.assertTrue(r1.result.complete)
        self.assertEqual(r1.result.observations[0].title, TITLE)

        self._serve_r2()
        _, r2 = self.collect("strict", "r2-strict")
        self.assertFalse(r2.result.complete)
        self.assertEqual(r2.result.stop_reason, "detail_failures")
        self.assertEqual(r2.result.observations, [])
        self.assertEqual(r2.result.details_failed, 1)
        decision = next(item for item in r2.result.decisions if item.kind == "detail")
        self.assertFalse(decision.accepted)
        self.assertFalse(decision.ambiguous)
        self.assertIsNone(decision.accepted_encoding)
        self.assertEqual(r2.lifecycle.new_jobs, 0)
        self.assertEqual(r2.lifecycle.changed_events, 0)
        self.assertEqual(store_events(self, "r2-strict"), {
            "NEW": 0, "CHANGED": 0, "DISAPPEARED": 0, "CLOSED": 0, "REOPENED": 0,
        })

        store = Store(self.db_path)
        try:
            row = store.find_job("jobtrace-demo", "7")
            self.assertEqual(row["title"], TITLE)
            self.assertEqual(row["missing_streak"], 0)
        finally:
            store.close()

    def test_raw_bytes_land_on_disk_even_when_strict_rejects(self):
        self._serve_r2()
        loaded, outcome = self.collect("strict", "r-raw")
        self.assertFalse(outcome.result.complete)
        records = list(RawStore(
            loaded.config.storage.raw_dir, self.clock
        ).iter_manifest())
        detail = next(record for record in records if record.url.endswith("/jobs/7"))
        raw = RawStore(loaded.config.storage.raw_dir, self.clock).read(detail.sha256)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), detail.sha256)
        self.assertEqual(raw, self.site.pages["/jobs/7"].body)
        self.assertIn(TITLE.encode(GB18030), raw)

    def test_decisions_keep_bom_header_meta_and_field_byte_positions(self):
        self._serve_r2()
        _, outcome = self.collect("reconcile", "r-evidence")
        decision = next(item for item in outcome.result.decisions if item.kind == "detail")
        self.assertEqual(decision.rule_version, RULE_VERSION)
        self.assertEqual(decision.http_charset, "utf-8")
        self.assertEqual(decision.meta_charset, "gb18030")
        self.assertIsNone(decision.bom_encoding)
        candidate = next(item for item in decision.candidates
                         if item.encoding == "gb18030" and item.lossless)
        title_span = next(span for span in candidate.fields if span.name == "title")
        company_span = next(span for span in candidate.fields if span.name == "company")
        self.assertIsNotNone(title_span.byte_start)
        self.assertIsNotNone(company_span.byte_start)
        raw = self.site.pages["/jobs/7"].body
        self.assertEqual(raw[title_span.byte_start:title_span.byte_end],
                         TITLE.encode(GB18030))
        self.assertEqual(raw[company_span.byte_start:company_span.byte_end],
                         COMPANY.encode(GB18030))
        strict_utf8 = next(item for item in decision.candidates
                           if item.encoding == "utf-8")
        self.assertFalse(strict_utf8.lossless)


def store_events(case: CharsetCase, run_id: str) -> dict[str, int]:
    store = Store(case.db_path)
    try:
        return store.event_counts(run_id)
    finally:
        store.close()
