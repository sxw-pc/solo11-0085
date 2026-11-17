"""Acceptance edges: replacement-char fakes, BOM recovery, mixed encodings."""

from __future__ import annotations

from jobtrace.charset import REPLACEMENT_CHAR, analyze, accepted_text
from jobtrace.config import DEFAULT_SELECTORS, load_selectors
from jobtrace.mock_site.charset_site import (
    GB18030, UTF8, build_detail_html, build_list_html,
)
from jobtrace.parser import parse_detail_page, parse_list_page
from jobtrace.rawstore import RawStore

from tests._charset_support import CharsetCase

SELECTORS = load_selectors(DEFAULT_SELECTORS)


class AcceptanceEdgeTest(CharsetCase):
    def test_replacement_char_text_is_never_accepted(self):
        body = "工程师".encode("gb18030")
        lossy = body.decode("utf-8", errors="replace")
        self.assertIn(REPLACEMENT_CHAR, lossy)
        # The old behavior would have found non-empty selectors on the lossy text.
        soup_text = build_detail_html(title=lossy, company=lossy,
                                      encoding="utf-8", meta_charset=None).decode("utf-8")
        old_style = parse_detail_page(soup_text, "http://127.0.0.1/jobs/7",
                                      "jobtrace-demo", _item(), SELECTORS)
        self.assertIsNotNone(old_style.observation)
        # The new acceptance gate rejects the original bytes.
        raw = build_detail_html(title="工程师", company="甲公司",
                                encoding=GB18030, meta_charset=None)
        for contract in ("strict", "reconcile"):
            with self.subTest(contract=contract):
                decision = analyze(
                    raw, sha256="x", url="http://127.0.0.1/jobs/7", kind="detail",
                    content_type="text/html; charset=utf-8",
                    contract=contract, selectors=SELECTORS,
                )
                self.assertFalse(decision.accepted)
                self.assertFalse(decision.ambiguous)
                self.assertTrue(all(
                    not item.lossless for item in decision.candidates
                    if item.encoding == "utf-8"
                ))

    def test_replacement_fake_runs_incomplete_and_persists_raw(self):
        raw = build_detail_html(title="工程师", company="甲公司",
                                encoding=GB18030, meta_charset=None)
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        self.site.set("/jobs/7", raw, "text/html; charset=utf-8")
        for contract in ("strict", "reconcile"):
            with self.subTest(contract=contract):
                loaded, outcome = self.collect(contract, f"ffd-{contract}")
                self.assertFalse(outcome.result.complete)
                self.assertEqual(outcome.result.observations, [])
                store = RawStore(loaded.config.storage.raw_dir, self.clock)
                records = list(store.iter_manifest())
                self.assertTrue(any(
                    record.url.endswith("/jobs/7") and store.read(record.sha256) == raw
                    for record in records
                ))

    def test_bom_recovers_when_header_mislabelled_under_reconcile(self):
        payload = build_detail_html(title="工程师", company="甲公司",
                                    encoding=UTF8, meta_charset=None)
        bom_body = b"\xef\xbb\xbf" + payload
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        self.site.set("/jobs/7", bom_body, "text/html; charset=iso-8859-1")
        _, strict = self.collect("strict", "bom-strict")
        self.assertFalse(strict.result.complete)
        _, reconcile = self.collect("reconcile", "bom-reconcile")
        self.assertTrue(reconcile.result.complete)
        decision = next(item for item in reconcile.result.decisions
                        if item.kind == "detail")
        self.assertEqual(decision.accepted_source, "bom")
        self.assertEqual(decision.bom_encoding, "utf-8-sig")
        self.assertEqual(reconcile.result.observations[0].title, "工程师")
        self.assertEqual(accepted_text(bom_body, decision).lstrip("﻿"),
                         bom_body.decode("utf-8-sig"))

    def test_list_utf8_detail_gb18030_are_accepted_per_page(self):
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        detail = build_detail_html(title="工程师", company="甲公司",
                                   encoding=GB18030, meta_charset=GB18030)
        self.site.set("/jobs/7", detail, "text/html; charset=gb18030")
        for contract in ("strict", "reconcile"):
            with self.subTest(contract=contract):
                _, outcome = self.collect(contract, f"mixed-{contract}")
                self.assertTrue(outcome.result.complete)
                by_kind = {item.kind: item for item in outcome.result.decisions}
                self.assertEqual(by_kind["list"].accepted_encoding, "utf-8")
                self.assertEqual(by_kind["detail"].accepted_encoding, "gb18030")
                observation = outcome.result.observations[0]
                self.assertEqual(observation.title, "工程师")
                self.assertEqual(observation.company, "甲公司")

    def test_reconcile_without_meta_or_bom_still_fails(self):
        raw = build_detail_html(title="工程师", company="甲公司",
                                encoding=GB18030, meta_charset=None)
        decision = analyze(
            raw, sha256="x", url="http://127.0.0.1/jobs/7", kind="detail",
            content_type="text/html; charset=utf-8",
            contract="reconcile", selectors=SELECTORS,
        )
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.meta_charset)
        self.assertIsNone(decision.bom_encoding)
        self.assertIn("no lossless", decision.reason)

    def test_unknown_charset_name_fails_both_contracts(self):
        raw = build_detail_html(title="工程师", company="甲公司",
                                encoding=GB18030, meta_charset=GB18030)
        decision = analyze(
            raw, sha256="x", url="http://127.0.0.1/jobs/7", kind="detail",
            content_type="text/html; charset=x-unknown-9999",
            contract="strict", selectors=SELECTORS,
        )
        self.assertIsNone(decision.http_charset)
        self.assertFalse(decision.accepted)
        reconciled = analyze(
            raw, sha256="x", url="http://127.0.0.1/jobs/7", kind="detail",
            content_type="text/html; charset=x-unknown-9999",
            contract="reconcile", selectors=SELECTORS,
        )
        self.assertTrue(reconciled.accepted)
        self.assertEqual(reconciled.accepted_source, "meta")

    def test_list_parse_external_id_and_detail_url(self):
        raw = build_list_html(meta_charset=UTF8)
        page = parse_list_page(raw.decode("utf-8"),
                               "http://127.0.0.1/jobs", SELECTORS,
                               source_encoding="utf-8")
        self.assertEqual(page.items[0].external_id, "7")
        self.assertTrue(page.items[0].detail_url.endswith("/jobs/7"))
        self.assertEqual(page.source_encoding, "utf-8")


def _item():
    from jobtrace.models import ListItem

    return ListItem(external_id="7", title="", company="", location="",
                    salary="", detail_url="http://127.0.0.1/jobs/7")
