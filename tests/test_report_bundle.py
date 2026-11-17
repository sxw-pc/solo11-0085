"""Report bundle publication: consistent snapshots, previews, recovery."""

from __future__ import annotations

import csv
import json
import pathlib
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from jobtrace import publication
from jobtrace.bundle import (FILENAMES, FORMATS, PUBLISHED_POINTER, Writes,
                             read_preview, read_published, verify_bundle)
from jobtrace.clock import FixedClock, parse_instant
from jobtrace.exporters import render_csv, render_json, render_jsonl, render_markdown
from jobtrace.lifecycle import LifecycleEngine
from jobtrace.models import JobObservation
from jobtrace.normalize import normalize_experience
from jobtrace.publication import publish_report, recover
from jobtrace.report import build_report, compute_identity, identity_payload
from jobtrace.salary import parse_salary
from jobtrace.store import Store

SITE, T1, T2 = "site", "2026-01-15T09:00:00Z", "2026-01-16T09:00:00Z"
CLOCK = FixedClock(parse_instant("2026-01-17T10:00:00Z"))


def _observation(external_id: str, title: str = "工程师",
                 salary: str = "10-20K") -> JobObservation:
    return JobObservation(
        site_key=SITE, external_id=external_id, title=title, company="合成公司",
        location_raw="北京 · 海淀区", city="北京", district="海淀区",
        salary=parse_salary(salary), experience=normalize_experience("3-5年"),
        employment_type="全职", description="", tags=("Python",),
        detail_url=f"http://127.0.0.1/jobs/{external_id}")


def _add_run(store: Store, run_id: str, started_at: str, observations, *,
             complete: bool = True, scenario: str = "baseline") -> None:
    store.begin_run(run_id=run_id, site_key=SITE, scenario=scenario, config_hash="cfg",
                    base_url="http://127.0.0.1:9", started_at=started_at)
    LifecycleEngine(store).apply(run_id=run_id, site_key=SITE,
                                 observations=observations, complete=complete,
                                 now=started_at, close_after_missing_runs=2)
    store.finish_run(run_id, status="complete" if complete else "incomplete",
                     is_complete=complete, finished_at=started_at,
                     unique_jobs=len(observations))


def _failing_markdown_write(real):
    def fake(self, data, *args, **kwargs):
        if self.name == "report.md":
            raise OSError("simulated failure before markdown")
        return real(self, data, *args, **kwargs)
    return fake


class BundleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.db, self.out = root / "db.sqlite3", root / "reports"
        with Store(self.db) as store:
            _add_run(store, "R1", T1, [_observation("J1"), _observation("J2")])
            _add_run(store, "R2", T2, [_observation("J1", "高级工程师", "20-30K"),
                                       _observation("J2"), _observation("J3")])
        self.store = Store(self.db, readonly=True)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def publish(self, run_id=None, writes=None):
        return publish_report(self.store, self.out, CLOCK, SITE, run_id=run_id,
                              writes=writes)
    def read_published_json(self):
        self.assertEqual(read_published(self.out)["status"], "published")
        return json.loads((self.out / FILENAMES["json"]).read_text(encoding="utf-8"))

    def add_run(self, run_id, started_at, observations, **kwargs):
        with Store(self.db) as store:
            _add_run(store, run_id, started_at, observations, **kwargs)


class InterleaveTests(BundleCase):
    def test_r2_json_written_then_markdown_fails_keeps_r1_published(self):
        self.assertEqual(self.publish(run_id="R1")["status"], "published")
        r1_state = read_published(self.out)
        before = {n: pathlib.Path(r1_state["formats"][n]).read_bytes() for n in FORMATS}
        with mock.patch.object(pathlib.Path, "write_text",
                               _failing_markdown_write(pathlib.Path.write_text)):
            second = self.publish(run_id="R2")
        self.assertEqual((second["status"], second["error"]), ("failed", "OSError"))
        state = read_published(self.out)
        self.assertEqual((state["bundle_id"], state["completed_formats"]),
                         (r1_state["bundle_id"], list(FORMATS)))
        self.assertEqual(self.read_published_json()["summary"]["run_id"], "R1")
        self.assertEqual({n: pathlib.Path(state["formats"][n]).read_bytes()
                          for n in FORMATS}, before)
        preview = read_preview(self.out, second["bundle_id"])
        self.assertEqual((preview["completed_formats"], preview["missing_formats"]),
                         (["json"], ["markdown", "csv", "jsonl"]))
        preview_doc = json.loads(pathlib.Path(preview["formats"]["json"]).read_text(
            encoding="utf-8"))
        self.assertEqual(preview_doc["summary"]["run_id"], "R2")
        self.assertNotEqual(preview_doc["bundle"]["bundle_id"], r1_state["bundle_id"])
        self.assertNotEqual(preview["formats"]["json"], state["formats"]["json"])
        retried = self.publish(run_id="R2")  # same-run retry completes the batch
        self.assertEqual((retried["status"], retried["bundle_id"]),
                         ("published", second["bundle_id"]))
        self.assertEqual(self.read_published_json()["summary"]["run_id"], "R2")

    def test_jobs_snapshot_cannot_switch_run_mid_build(self):
        class InterleavingStore(Store):
            done = False

            def list_jobs(self, **kwargs):
                if not InterleavingStore.done:  # R3 commits mid-build.
                    InterleavingStore.done = True
                    with Store(self.path) as writer:
                        _add_run(writer, "R3", "2026-01-17T09:00:00Z",
                                 [_observation("J1"), _observation("J4")])
                return super().list_jobs(**kwargs)

        with InterleavingStore(self.db, readonly=True) as store:
            report = build_report(store, CLOCK, SITE)
        self.assertEqual(report["summary"]["run_id"], "R2")
        self.assertEqual({job["external_id"] for job in report["jobs"]},
                         {"J1", "J2", "J3"})
        with Store(self.db, readonly=True) as store:
            latest = build_report(store, CLOCK, SITE)
        self.assertEqual(latest["summary"]["run_id"], "R3")
        self.assertIn("J4", {job["external_id"] for job in latest["jobs"]})


class OrderingTests(BundleCase):
    def test_same_run_retry_never_rewrites_verified_candidate(self):
        first = self.publish(run_id="R1")
        target = pathlib.Path(first["paths"]["json"])
        before, writes = target.read_bytes(), Writes()
        retry = self.publish(run_id="R1", writes=writes)
        self.assertEqual((retry["status"], retry["bundle_id"]),
                         ("published", first["bundle_id"]))
        self.assertEqual((target.read_bytes(), writes.calls), (before, []))

    def test_later_run_publishes_but_older_run_never_moves_pointer_back(self):
        self.publish(run_id="R1")
        second = self.publish(run_id="R2")
        self.assertEqual(self.read_published_json()["summary"]["run_id"], "R2")
        self.assertEqual(self.publish(run_id="R1")["status"], "held")
        self.assertEqual(read_published(self.out)["bundle_id"], second["bundle_id"])

    def test_database_change_during_collection_is_held_not_published(self):
        original = build_report

        def build_then_finish_again(store, clock, site_key=None, *, run_id=None):
            report = original(store, clock, site_key, run_id=run_id)
            with Store(self.db) as writer:
                writer.finish_run("R2", status="complete", is_complete=True,
                                  finished_at="2026-01-16T11:00:00Z", unique_jobs=99)
            return report

        with mock.patch("jobtrace.publication.report_module.build_report",
                        build_then_finish_again):
            result = self.publish(run_id="R2")
        self.assertEqual((result["status"], result["published"]["status"],
                          read_preview(self.out, result["bundle_id"])["status"]),
                         ("held", "empty", "candidate_complete"))
        self.assertFalse((self.out / PUBLISHED_POINTER).exists())

    def test_incomplete_run_is_published_as_explicitly_incomplete(self):
        self.add_run("R3", "2026-01-17T09:00:00Z", [_observation("J1")],
                     complete=False, scenario="malformed")
        self.assertEqual(self.publish(run_id="R3")["status"], "published")
        report = self.read_published_json()
        self.assertFalse(report["bundle"]["observation_complete"])
        self.assertEqual(report["bundle"]["observation_scope"], "incomplete")
        markdown = (self.out / FILENAMES["markdown"]).read_text(encoding="utf-8")
        self.assertIn("不代表完整岗位集合", markdown)


class ContractTests(BundleCase):
    def test_empty_and_failed_generation_report_explicit_status(self):
        self.assertEqual((read_published(self.out)["status"],
                          read_preview(self.out)["status"]), ("empty", "empty"))
        with mock.patch.object(pathlib.Path, "write_text",
                               _failing_markdown_write(pathlib.Path.write_text)):
            result = self.publish(run_id="R1")
        self.assertEqual((result["status"], result["published"]["status"],
                          result["preview"]["status"]),
                         ("failed", "empty", "candidate_partial"))
        self.assertFalse((self.out / FILENAMES["markdown"]).exists())
        self.assertFalse((self.out / PUBLISHED_POINTER).exists())


    def test_four_formats_share_identity_and_row_sets(self):
        self.publish(run_id="R2")
        report = self.read_published_json()
        bundle = report["bundle"]
        self.assertEqual(bundle["bundle_id"], "bundle-" + bundle["content_digest"][:24])
        unsigned = {**report, "bundle": dict(bundle, bundle_id=None, content_digest=None)}
        self.assertEqual(compute_identity(unsigned),
                         (bundle["bundle_id"], bundle["content_digest"]))
        self.assertEqual(identity_payload(report)["jobs"], report["jobs"])
        read = lambda name: (self.out / FILENAMES[name]).read_text(encoding="utf-8")
        csv_rows = list(csv.DictReader(read("csv").splitlines()))
        jsonl_rows = [json.loads(line) for line in read("jsonl").splitlines()]
        markdown = read("markdown")
        self.assertEqual({row["external_id"] for row in csv_rows},
                         {job["external_id"] for job in report["jobs"]})
        self.assertEqual([row["external_id"] for row in jsonl_rows],
                         [event["external_id"] for event in report["events"]])
        for row in csv_rows + jsonl_rows:
            self.assertEqual((row["bundle_id"], row["content_digest"]),
                             (bundle["bundle_id"], bundle["content_digest"]))
        self.assertIn(bundle["bundle_id"], markdown)
        self.assertIn(bundle["content_digest"], markdown)
        for name, rendered in (("json", render_json(report)),
                               ("markdown", render_markdown(report) + "\n"),
                               ("csv", render_csv(report)),
                               ("jsonl", render_jsonl(report))):
            self.assertEqual(rendered, read(name))
        verify_bundle(self.out / "bundles" / bundle["bundle_id"], bundle["bundle_id"])


class RecoveryTests(BundleCase):
    def test_recovery_repairs_journal_from_committed_link(self):
        self.publish(run_id="R1")
        second = self.publish(run_id="R2")
        (self.out / PUBLISHED_POINTER).unlink()
        state = read_published(self.out)
        self.assertEqual((state["bundle_id"], state["pointer_status"]),
                         (second["bundle_id"], "needs_recovery"))
        result = recover(self.out, self.store)
        self.assertEqual((result["status"], result["repaired"], result["verified"]),
                         ("published", True, True))
        self.assertEqual(read_published(self.out)["bundle_id"], second["bundle_id"])
        self.assertEqual(read_published(self.out)["pointer_status"], "published")

    def test_crash_before_link_switch_keeps_previous_published_bundle(self):
        first, second = self.publish(run_id="R1"), self.publish(run_id="R2")
        link = self.out / "current"
        link.unlink()
        link.symlink_to(f"bundles/{first['bundle_id']}")
        self.assertEqual(read_published(self.out)["bundle_id"], first["bundle_id"])
        recover(self.out, self.store)
        self.assertEqual(read_published(self.out)["bundle_id"], first["bundle_id"])
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])

    def test_leftover_files_are_not_treated_as_published(self):
        self.publish(run_id="R1")
        published_before = read_published(self.out)["bundle_id"]
        (self.out / "bundles" / "bundle-deadbeef").mkdir(parents=True)
        for name in ("report.md", "report.json"):
            (self.out / "bundles" / "bundle-deadbeef" / name).write_text("{}")
        (self.out / ".published.json.tmp-999").write_text("{}")
        result = recover(self.out, self.store)
        self.assertEqual(result["status"], "published")
        self.assertIn("bundle-deadbeef", result["abandoned"])
        self.assertEqual(read_published(self.out)["bundle_id"], published_before)
        preview = read_preview(self.out, "bundle-deadbeef")
        self.assertEqual((preview["completed_formats"], preview["missing_formats"]),
                         ([], list(FORMATS)))
        self.assertFalse(any(p.name == ".published.json.tmp-999"
                             for p in self.out.iterdir()))

    def test_journal_write_failure_after_link_switch_is_recoverable(self):
        real_atomic = publication.write_atomic

        def fail_pointer(path, content):
            if path.name == PUBLISHED_POINTER:
                raise OSError("simulated journal failure")
            return real_atomic(path, content)
        self.publish(run_id="R1")
        with mock.patch.object(publication, "write_atomic", fail_pointer):
            result = self.publish(run_id="R2")
        self.assertEqual((result["status"], read_published(
            self.out)["pointer_status"]), ("held", "needs_recovery"))
        self.assertEqual(read_preview(self.out, result["bundle_id"])["status"],
                         "candidate_complete")
        recovered = recover(self.out, self.store)
        self.assertEqual((recovered["status"], recovered["bundle_id"]),
                         ("published", result["bundle_id"]))
        self.assertEqual(self.read_published_json()["summary"]["run_id"], "R2")
        # A rerun of R2 is idempotent; a newer run still moves forward, and
        # replaying R2 afterwards can never regress the committed pointer.
        self.assertEqual(self.publish(run_id="R2")["status"], "published")
        self.add_run("R3", "2026-01-18T09:00:00Z",
                     [_observation("J1"), _observation("J5")])
        third = self.publish(run_id="R3")
        self.assertEqual(self.publish(run_id="R2")["status"], "held")
        self.assertEqual(read_published(self.out)["bundle_id"], third["bundle_id"])


if __name__ == "__main__":
    unittest.main()