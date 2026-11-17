"""Snapshot-epoch list evidence, chain proofs, and the two collection contracts.

The synthetic epoch site declares snapshot_id / page index / total pages on every
page and lets a test mutate those declarations, so each corruption below is a
small, explicit edit rather than a parser accident.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest

from epoch_harness import FIXED, epoch_environment

from jobtrace.http_client import HttpResponse
from jobtrace.mock_site.epoch_site import SnapshotPage, _list_html
from jobtrace.page_contracts import verdict_for
from jobtrace.page_epoch import EvidenceLedger
from jobtrace.parser import page_observation, parse_list_page


def _render(snapshot_id, index, total, ids, *, next_url=None, url_page=None):
    """Render one declared page and bind it to a fake response, outside the site."""

    page = SnapshotPage(snapshot_id, index, total,
                        tuple({"id": job_id, "title": job_id} for job_id in ids),
                        next_url=next_url, url_page=url_page)
    html = _list_html(page)
    raw = html.encode("utf-8")
    declared = url_page if url_page is not None else index
    url = f"http://127.0.0.1:9/jobs?page={declared}"
    response = HttpResponse(
        url=url, status=200, content=raw, text=html, content_type="text/html",
        sha256=hashlib.sha256(raw).hexdigest(), attempts=1, fetched_at=FIXED)
    return response, url


class PageEpochEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def loaded(self):
        with epoch_environment(self._tmp.name) as env:
            return env.loaded

    def ledger(self):
        return EvidenceLedger(self.loaded().config.site.page_param)

    def observe(self, ledger, response, url):
        loaded = self.loaded()
        parsed = parse_list_page(response.text, url, loaded.selectors)
        ledger.observe(page_observation(response, url, parsed))

    def test_s1_page1_then_s2_page2_proves_neither_snapshot(self):
        ledger = self.ledger()
        for snapshot, index, ids in (("S1", 1, ("S1-A", "S1-B")), ("S2", 2, ("S2-C",))):
            response, url = _render(snapshot, index, 2, ids,
                                    next_url="/jobs?page=2" if index == 1 else None)
            self.observe(ledger, response, url)
        fixed = verdict_for("fixed_snapshot", ledger)
        live = verdict_for("live_observation", ledger)
        self.assertFalse(fixed.complete)
        self.assertEqual(fixed.stop_reason, "snapshot_switch")
        self.assertEqual(fixed.items, [])
        self.assertEqual([s["snapshot_id"] for s in live.segments], ["S1", "S2"])
        self.assertFalse(fixed.claims_all_jobs or live.claims_all_jobs)

    def test_live_s2_is_only_completed_by_a_proven_link(self):
        ledger = self.ledger()
        for index, ids, nxt in ((2, ("S2-C",), None), (1, ("S2-A", "S2-B"), "/jobs?page=2")):
            response, url = _render("S2", index, 2, ids, next_url=nxt)
            self.observe(ledger, response, url)
        self.assertTrue(verdict_for("live_observation", ledger).segments[0]["complete"])

        broken = self.ledger()
        for index, ids, nxt in ((2, ("S2-C",), None), (1, ("S2-A",), "/jobs?page=3")):
            response, url = _render("S2", index, 2, ids, next_url=nxt)
            self.observe(broken, response, url)
        segment = verdict_for("live_observation", broken).segments[0]
        self.assertFalse(segment["complete"])
        self.assertEqual(segment["coverage_ranges"], [[1, 1], [2, 2]])

    def test_missing_page_breaks_fixed_contract(self):
        ledger = self.ledger()
        for index, total, ids, nxt in ((1, 3, ("A",), "/jobs?page=3"),
                                       (3, 3, ("C",), None)):
            response, url = _render("S1", index, total, ids, next_url=nxt)
            self.observe(ledger, response, url)
        fixed = verdict_for("fixed_snapshot", ledger)
        self.assertFalse(fixed.complete)
        self.assertEqual(fixed.segments[0]["missing_pages"], [2])

    def test_total_pages_conflict_is_kept_as_witness(self):
        ledger = self.ledger()
        for index, total, nxt in ((1, 3, "/jobs?page=2"), (2, 4, "/jobs?page=3")):
            response, url = _render("S1", index, total, ("A",), next_url=nxt)
            self.observe(ledger, response, url)
        fixed = verdict_for("fixed_snapshot", ledger)
        self.assertFalse(fixed.complete)
        details = [item["detail"] for item in fixed.witnesses]
        self.assertTrue(any("total_pages" in detail for detail in details))
        self.assertTrue(all(len(item["response_digest"]) == 64 for item in fixed.witnesses))

    def test_duplicate_page_and_same_url_change_are_conflicts(self):
        ledger = self.ledger()
        first, first_url = _render("S1", 1, 2, ("A",), next_url="/jobs?page=2")
        self.observe(ledger, first, first_url)
        second, second_url = _render("S1", 1, 2, ("A", "B"), next_url="/jobs?page=2")
        self.observe(ledger, second, second_url)
        witnesses = verdict_for("fixed_snapshot", ledger).witnesses
        kinds = {item["kind"] for item in witnesses}
        self.assertIn("duplicate_page", kinds)
        self.assertIn("content_changed", kinds)
        self.assertEqual(len({item.sha256 for item in ledger.by_url[first_url]}), 2)
        changed = [item for item in witnesses if item["kind"] == "content_changed"]
        self.assertTrue(changed and changed[0]["prior"])

    def test_back_link_and_page_skip_are_conflicts(self):
        ledger = self.ledger()
        response, url = _render("S1", 1, 3, ("A",), next_url="/jobs?page=1")
        self.observe(ledger, response, url)
        fixed = verdict_for("fixed_snapshot", ledger)
        self.assertFalse(fixed.complete)
        self.assertTrue(any("backward" in item["detail"] or "URL declares" in item["detail"]
                            for item in fixed.witnesses))

        skipped = self.ledger()
        for index, nxt, url_page in ((2, "/jobs?page=3", None), (3, None, 4)):
            response, url = _render("S1", index, 3, ("B",), next_url=nxt,
                                    url_page=url_page)
            self.observe(skipped, response, url)
        self.assertFalse(verdict_for("fixed_snapshot", skipped).complete)


if __name__ == "__main__":
    unittest.main()
