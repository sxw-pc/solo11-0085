"""Crawler-level snapshot epochs: contracts, resume re-validation, publication.

These tests drive the real crawler against the controllable synthetic epoch site,
so a page flip between requests is a genuine two-response HTTP sequence rather
than a hand-built ledger.
"""

from __future__ import annotations

import json
import tempfile
import unittest

from epoch_harness import FIXED, epoch_environment

from jobtrace.errors import PaginationError
from jobtrace.lifecycle import LifecycleEngine
from jobtrace.mock_site.epoch_site import SnapshotPage
from jobtrace.pagination import Cursor
from jobtrace.store import Store


class _Interrupt(Exception):
    """Raised from the request hook to simulate a crash mid-list-crawl."""


def _stop_before(page: int):
    marker = f"page={page}"

    def hook(url, _response):
        if url.endswith(marker):
            raise _Interrupt

    return hook


class PageContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = epoch_environment(self._tmp.name)
        self.env = self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)

    def test_switch_between_page1_and_page2_fails_fixed_contract(self):
        self.env.site.switch_after(1, "S2")

        def hook(url, _response):
            if url.endswith("page=2"):
                self.env.site.activate("S2")

        fixed = self.env.run("fixed_snapshot", hook=hook)
        self.assertFalse(fixed.complete)
        self.assertEqual(fixed.stop_reason, "snapshot_switch")
        self.assertEqual(fixed.unique_jobs, 3)  # S1 p1 + S2 p2, not a snapshot
        self.assertEqual(fixed.observations, [])
        self.assertEqual(fixed.list_verdict["items"], [])

        self.env.site.clear_controls()
        self.env.site.activate("S2")
        self.env.site.switch_after(1, "S1")
        live = self.env.run("live_observation", hook=hook)
        self.assertFalse(live.complete)
        snapshots = {s["snapshot_id"] for s in live.list_verdict["segments"]}
        self.assertEqual(snapshots, {"S1", "S2"})
        self.assertFalse(live.list_verdict["claims_all_jobs"])

    def test_resume_revalidates_prefix_and_cursor_is_atomic(self):
        self.env.site.override_next(2, "S1")
        with self.assertRaises(_Interrupt):
            self.env.run("fixed_snapshot", hook=_stop_before(2))
        cursor = Cursor.load(self.env.loaded.config.storage.checkpoint)
        self.assertIsNotNone(cursor)
        self.assertTrue(cursor.pages and cursor.next_url)
        self.assertEqual(cursor.snapshot_id, "S1")
        for page in cursor.pages:
            self.assertTrue({"sha256", "page_index", "snapshot_id"} <= set(page))

        # The site flips to S2 before resume; the old next_url must not be trusted.
        self.env.site.clear_controls()
        self.env.site.activate("S2")
        resumed = self.env.run("fixed_snapshot", resume=True)
        self.assertTrue(resumed.resumed)
        self.assertFalse(resumed.complete)
        self.assertEqual(resumed.stop_reason, "resumed_partial")
        self.assertEqual(resumed.observations, [])
        self.assertIn("resume prefix diverged", " ".join(resumed.warnings))

    def test_resume_after_same_url_change_does_not_overwrite(self):
        self.env.site.override_next(2, "S1")
        with self.assertRaises(_Interrupt):
            self.env.run("fixed_snapshot", hook=_stop_before(2))
        self.env.site.replace_page(SnapshotPage(
            "S1", 1, 2, ({"id": "S1-X", "title": "changed"},),
            next_url="/jobs?page=2", marker="mutated"))
        resumed = self.env.run("fixed_snapshot", resume=True)
        self.assertFalse(resumed.complete)
        changed = [item for item in resumed.list_verdict["witnesses"]
                   if item["kind"] == "content_changed"]
        self.assertTrue(changed and changed[0]["sha256"])

    def test_clean_resume_revalidates_but_stays_partial(self):
        self.env.site.override_next(2, "S1")
        with self.assertRaises(_Interrupt):
            self.env.run("fixed_snapshot", hook=_stop_before(2))
        # Nothing changed, so the saved prefix re-validates and the crawl
        # finishes the chain; it still must not claim a complete lifecycle round.
        resumed = self.env.run("fixed_snapshot", resume=True)
        self.assertTrue(resumed.resumed)
        self.assertFalse(resumed.complete)
        self.assertEqual(resumed.stop_reason, "resumed_partial")
        # The list chain itself is proven, but the run is still not a complete
        # lifecycle round, so it cannot close jobs it never re-observed.
        self.assertTrue(resumed.list_verdict["complete"])
        self.assertEqual(resumed.list_verdict["items"], ["S1-A", "S1-B", "S1-C"])

    def test_live_segments_never_merge_and_never_close_jobs(self):
        self.env.site.switch_after(1, "S2")

        def hook(url, _response):
            if url.endswith("page=2"):
                self.env.site.activate("S2")

        live = self.env.run("live_observation", hook=hook)
        segments = {s["snapshot_id"]: s for s in live.list_verdict["segments"]}
        self.assertEqual(set(segments), {"S1", "S2"})
        self.assertEqual(segments["S1"]["coverage_ranges"], [[1, 1]])
        self.assertEqual(segments["S2"]["coverage_ranges"], [[2, 2]])
        self.assertFalse(live.complete)
        self.assertFalse(live.list_verdict["claims_all_jobs"])
        self.assertEqual(live.list_verdict["items"], [])

    def test_live_s2_completes_only_with_a_proven_link(self):
        self.env.site.activate("S2")
        self.env.site.pin(2, "S2")
        live = self.env.run("live_observation")
        s2 = next(s for s in live.list_verdict["segments"] if s["snapshot_id"] == "S2")
        self.assertTrue(s2["complete"])
        self.assertEqual([item for span in s2["coverage_ranges"] for item in span], [1, 2])
        self.assertEqual(s2["range_items"], {"1-2": ["S2-A", "S2-B", "S2-C"]})

        # S2 page 2 served while S1 is active: ids match but the link does not.
        self.env.site.clear_controls()
        self.env.site.activate("S1")
        self.env.site.pin(2, "S2")
        split = self.env.run("live_observation")
        self.assertTrue(all(not s["complete"] for s in split.list_verdict["segments"]))
        self.assertFalse(split.list_verdict["claims_all_jobs"])

    def test_fixed_contract_requires_whole_chain_not_natural_end(self):
        self.env.site.replace_page(SnapshotPage(
            "S1", 1, 3, ({"id": "S1-A", "title": "a"},), next_url="/jobs?page=2"))
        self.env.site.replace_page(SnapshotPage(
            "S1", 2, 3, ({"id": "S1-C", "title": "c"},),
            prev_url="/jobs?page=1", marker="short"))
        fixed = self.env.run("fixed_snapshot")
        self.assertFalse(fixed.complete)
        self.assertEqual(fixed.stop_reason, "incomplete_chain")
        self.assertEqual(fixed.list_verdict["segments"][0]["missing_pages"], [3])
        self.assertFalse(fixed.list_verdict["claims_all_jobs"])

        self.env.site.replace_page(SnapshotPage(
            "S1", 1, 2, ({"id": "S1-A", "title": "a"},), next_url="/jobs?page=2"))
        self.env.site.replace_page(SnapshotPage(
            "S1", 2, 2, ({"id": "S1-C", "title": "c"},), prev_url="/jobs?page=1"))
        healed = self.env.run("fixed_snapshot")
        self.assertTrue(healed.complete)
        self.assertTrue(healed.list_verdict["claims_all_jobs"])

    def test_failed_fixed_round_keeps_published_jobs_reviewable(self):
        loaded = self.env.loaded
        store = Store(loaded.config.storage.database)
        try:
            store.begin_run(run_id="run-ok", site_key=loaded.config.site.name,
                            scenario="epoch", base_url=loaded.config.site.base_url,
                            config_hash="x", started_at=FIXED)
            first = self.env.run("fixed_snapshot")
            LifecycleEngine(store).apply(
                run_id="run-ok", site_key=loaded.config.site.name,
                observations=first.observations, complete=True, now=FIXED,
                close_after_missing_runs=2)
            store.finish_run("run-ok", status="complete", is_complete=True,
                             finished_at=FIXED)
            before = {row["external_id"] for row in store.list_jobs()}
            self.assertEqual(before, {"S1-A", "S1-B", "S1-C"})

            self.env.site.switch_after(1, "S2")
            broken = self.env.run("fixed_snapshot")
            store.begin_run(run_id="run-broken", site_key=loaded.config.site.name,
                            scenario="epoch", base_url=loaded.config.site.base_url,
                            config_hash="x", started_at=FIXED)
            late = LifecycleEngine(store).apply(
                run_id="run-broken", site_key=loaded.config.site.name,
                observations=broken.observations, complete=broken.complete, now=FIXED,
                close_after_missing_runs=2)
            self.assertEqual((late.disappeared, late.closed), (0, 0))
            self.assertEqual({row["external_id"] for row in store.list_jobs()}, before)
            self.assertEqual(store.event_counts("run-broken")["DISAPPEARED"], 0)
            self.assertFalse(broken.list_verdict["complete"])
        finally:
            store.close()


class CursorEvidenceTests(unittest.TestCase):
    def test_cursor_load_rejects_malformed_page_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/cursor.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 1, "site_key": "s", "next_url": "/jobs?page=2",
                    "visited_pages": [], "page_count": 0, "completed": False,
                    "updated_at": FIXED, "pages": ["not-an-object"],
                }, handle)
            with self.assertRaises(PaginationError):
                Cursor.load(path)


if __name__ == "__main__":
    unittest.main()
