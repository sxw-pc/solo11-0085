"""Dual-decodable conflict: both encodings are lossless but fields differ."""

from __future__ import annotations

from jobtrace.mock_site.charset_site import (
    GB18030, UTF8, build_detail_html, build_list_html,
)
from jobtrace.store import Store

from tests._charset_support import CharsetCase

DUAL = bytes.fromhex("e48285e58688")
UTF8_TEXT = DUAL.decode("utf-8")
GB_TEXT = DUAL.decode("gb18030")


class AmbiguousTest(CharsetCase):
    def _serve(self):
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        body = build_detail_html(title=DUAL.decode("utf-8"),
                                 company=DUAL.decode("utf-8"),
                                 encoding=UTF8, meta_charset=GB18030)
        self.site.set("/jobs/7", body, "text/html; charset=utf-8")
        return body

    def test_both_contracts_refuse_unadjudicated_conflict(self):
        body = self._serve()
        self.assertIn("鋫", body.decode("gb18030"))
        for contract in ("strict", "reconcile"):
            with self.subTest(contract=contract):
                _, outcome = self.collect(contract, f"amb-{contract}")
                self.assertFalse(outcome.result.complete)
                self.assertEqual(outcome.result.observations, [])
                decision = next(
                    item for item in outcome.result.decisions if item.kind == "detail"
                )
                self.assertTrue(decision.ambiguous)
                self.assertFalse(decision.accepted)
                self.assertIsNone(decision.accepted_encoding)
                lossless = {item.encoding for item in decision.candidates
                            if item.lossless}
                self.assertIn("utf-8", lossless)
                self.assertIn("gb18030", lossless)
                self.assertEqual(outcome.lifecycle.new_jobs, 0)
                self.assertEqual(outcome.lifecycle.changed_events, 0)

    def test_conflict_does_not_overwrite_existing_ledger_row(self):
        # Establish job 7 first with honest headers.
        body = build_detail_html(title="工程师", company="甲公司",
                                 encoding=GB18030, meta_charset=GB18030)
        self.site.set("/jobs", build_list_html(meta_charset=UTF8),
                      "text/html; charset=utf-8")
        self.site.set("/jobs/7", body, "text/html; charset=gb18030")
        _, first = self.collect("reconcile", "amb-base")
        self.assertTrue(first.result.complete)
        # Then the same run id is irrelevant: switch to conflicting bytes.
        self._serve()
        _, second = self.collect("reconcile", "amb-conflict")
        self.assertFalse(second.result.complete)
        store = Store(self.db_path)
        try:
            row = store.find_job("jobtrace-demo", "7")
            self.assertEqual(row["title"], "工程师")
            self.assertEqual(row["company"], "甲公司")
            self.assertEqual(store.event_counts("amb-conflict"), {
                "NEW": 0, "CHANGED": 0, "DISAPPEARED": 0, "CLOSED": 0,
                "REOPENED": 0,
            })
        finally:
            store.close()

    def test_dual_fixture_contract(self):
        self.assertNotEqual(UTF8_TEXT, GB_TEXT)
        self.assertEqual(UTF8_TEXT.encode("utf-8"), DUAL)
        self.assertEqual(GB_TEXT.encode("gb18030"), DUAL)
