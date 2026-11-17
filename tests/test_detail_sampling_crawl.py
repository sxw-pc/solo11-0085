"""End-to-end bounded detail sampling through the loopback mock site."""

from __future__ import annotations

import unittest

from jobtrace.clock import FixedClock, parse_instant
from jobtrace.config import load_config
from jobtrace.mock_site import create_app
from jobtrace.pipeline import run_collection
from jobtrace.report import build_report, render_markdown
from jobtrace.sampling import SamplingRequest, build_frame, plan_probability
from jobtrace.serving import start_server
from jobtrace.store import Store

FIXED = "2026-01-15T09:00:00Z"
COMPANY_A = "示例公司甲(虚构)"
COMPANY_B = "星砂科技(虚构)"


def seed_with(tc: "FourCardCrawlTest", ids: set[str]) -> int:
    """Find a seed whose recorded selection equals ``ids``; verify via the run itself."""
    for seed in range(100):
        outcome = tc.run_with(SamplingRequest("probability", seed=seed),
                              f"run-probe-{seed}")
        selected = {entry["external_id"] for entry in outcome.result.sampling["selected"]}
        if selected == ids:
            return seed
    raise AssertionError(f"no probed seed selects {sorted(ids)}")


class FourCardCrawlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = start_server(create_app("quad"), port=0)
        self.addCleanup(self.server.stop)
        self.loaded = load_config(overrides={
            "site.base_url": f"http://127.0.0.1:{self.server.port}",
            "site.allowed_hosts": ["127.0.0.1"],
            "site.allowed_ports": [self.server.port],
            "site.max_details": 2,
            "storage.database": f"{self.tmp.name}/db.sqlite3",
            "storage.raw_dir": f"{self.tmp.name}/raw",
            "storage.report_dir": f"{self.tmp.name}/reports",
            "storage.checkpoint": f"{self.tmp.name}/checkpoint.json",
            "clock.fixed_at": FIXED,
        }, environ={})

    def run_with(self, request: SamplingRequest, run_id: str):
        return run_collection(
            self.loaded, scenario="quad", run_id=run_id, sampling=request,
            sleep=lambda _seconds: None,
        )

    def test_targeted_contract_covers_only_company_a_and_never_infers_a_ratio(self):
        outcome = self.run_with(SamplingRequest("targeted", ("A1", "A2")), "run-targeted")
        result, sampling = outcome.result, outcome.result.sampling
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "detail_limit")
        self.assertEqual(sampling["contract"], "targeted")
        self.assertIsNone(sampling["estimate"])
        self.assertIsNone(sampling["bounds"])
        self.assertEqual(sampling["counts"]["detail_attempted"], 2)
        self.assertEqual(sampling["counts"]["verified_positive"], 0)
        self.assertEqual(sampling["uncovered_ids"], ["B1", "B2"])
        self.assertEqual(sampling["uncovered_strata"], {COMPANY_B: 2})
        self.assertFalse(sampling["claims"]["whole_page_unbiased_proportion"])
        self.assertEqual(sampling["claims"]["coverage_scope"], "targeted_ids_only")
        self.assertTrue(sampling["audit"]["ok"])
        self.assertNotIn("B1", [entry["external_id"] for entry in sampling["selected"]])

    def test_probability_contract_covers_both_companies_and_labels_an_estimate(self):
        seed = seed_with(self, {"A1", "B1"})
        outcome = self.run_with(SamplingRequest("probability", seed=seed), "run-prob")
        result, sampling = outcome.result, outcome.result.sampling
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "detail_limit")
        self.assertEqual(sampling["seed"], seed)
        self.assertEqual([entry["external_id"] for entry in sampling["selected"]],
                         ["A1", "B1"])
        self.assertEqual(sampling["allocations"], {COMPANY_A: 1, COMPANY_B: 1})
        self.assertEqual(sampling["counts"]["verified"], 2)
        self.assertEqual(sampling["counts"]["verified_positive"], 1)
        self.assertEqual(sampling["estimate"]["intern_count"], 2.0)
        self.assertIn("design-weighted", sampling["estimate"]["label"])
        self.assertEqual(sampling["bounds"]["intern_count"], [1, 3])
        self.assertFalse(sampling["variance_estimable"])
        self.assertIsNone(sampling["confidence_interval"])
        self.assertTrue(sampling["variance_reason"])
        self.assertTrue(sampling["claims"]["whole_page_unbiased_proportion"])
        self.assertFalse(sampling["claims"]["details_complete"])
        self.assertEqual(sampling["claims"]["coverage_scope"],
                         "selected_probability_sample")

    def test_unknown_employment_type_is_not_counted_as_non_intern(self):
        server = start_server(create_app("quad_unknown"), port=0)
        self.addCleanup(server.stop)
        loaded = load_config(overrides={
            "site.base_url": f"http://127.0.0.1:{server.port}",
            "site.allowed_hosts": ["127.0.0.1"],
            "site.allowed_ports": [server.port],
            "site.max_details": 2,
            "storage.database": f"{self.tmp.name}/unknown.sqlite3",
            "storage.raw_dir": f"{self.tmp.name}/raw2",
            "storage.report_dir": f"{self.tmp.name}/reports2",
            "storage.checkpoint": f"{self.tmp.name}/checkpoint2.json",
            "clock.fixed_at": FIXED,
        }, environ={})
        seed = 2  # selects A1 (non-intern) and B1 (employment_type omitted)
        outcome = run_collection(
            loaded, scenario="quad_unknown", run_id="run-unknown",
            sampling=SamplingRequest("probability", seed=seed),
            sleep=lambda _seconds: None,
        )
        sampling = outcome.result.sampling
        self.assertEqual(sampling["counts"]["verified"], 1)
        self.assertEqual(sampling["counts"]["unknown_field"], 1)
        self.assertEqual(sampling["counts"]["detail_failed"], 0)
        self.assertNotIn(sampling["estimate"]["intern_count"], (0.0, 2.0))
        self.assertFalse(sampling["claims"]["whole_page_unbiased_proportion"])

    def test_failed_detail_stays_unknown_and_weights_survive_a_rerun(self):
        server = start_server(create_app("quad_404"), port=0)
        self.addCleanup(server.stop)
        loaded = load_config(overrides={
            "site.base_url": f"http://127.0.0.1:{server.port}",
            "site.allowed_hosts": ["127.0.0.1"],
            "site.allowed_ports": [server.port],
            "site.max_details": 2,
            "storage.database": f"{self.tmp.name}/miss.sqlite3",
            "storage.raw_dir": f"{self.tmp.name}/raw3",
            "storage.report_dir": f"{self.tmp.name}/reports3",
            "storage.checkpoint": f"{self.tmp.name}/checkpoint3.json",
            "clock.fixed_at": FIXED,
        }, environ={})
        seed = None
        for candidate in range(100):
            probe = run_collection(
                loaded, scenario="quad_404", run_id=f"run-probe-{candidate}",
                sampling=SamplingRequest("probability", seed=candidate),
                sleep=lambda _seconds: None,
            ).result.sampling
            if {entry["external_id"] for entry in probe["selected"]} == {"A1", "B1"}:
                seed = candidate
                break
        self.assertIsNotNone(seed)
        runs = []
        for run_id in ("run-miss-1", "run-miss-2"):
            runs.append(run_collection(
                loaded, scenario="quad_404", run_id=run_id,
                sampling=SamplingRequest("probability", seed=seed),
                sleep=lambda _seconds: None,
            ).result.sampling)
        first, second = runs
        self.assertEqual([entry["external_id"] for entry in first["selected"]],
                         ["A1", "B1"])
        self.assertEqual(first["counts"]["detail_failed"], 1)
        self.assertEqual(first["counts"]["verified"], 1)
        self.assertEqual(first["counts"]["verified_positive"], 0)
        self.assertEqual(first["allocations"], second["allocations"])
        self.assertEqual(first["frame"]["digest"], second["frame"]["digest"])
        for left, right in zip(first["selected"], second["selected"]):
            self.assertEqual(left["inclusion_probability"], right["inclusion_probability"])
        self.assertIsNone(first["estimate"]["intern_count"])
        self.assertFalse(first["claims"]["whole_page_unbiased_proportion"])
        self.assertEqual(first["bounds"]["intern_count"], [0, 3])

    def test_report_states_counts_scope_and_estimate_for_the_latest_run(self):
        seed = seed_with(self, {"A1", "B1"})
        self.run_with(SamplingRequest("probability", seed=seed), "run-report")
        store = Store(self.loaded.config.storage.database, readonly=True)
        try:
            report = build_report(store, FixedClock(parse_instant(FIXED)), "jobtrace-demo")
        finally:
            store.close()
        sampling = report["latest_run"]["sampling"]
        self.assertEqual(sampling["contract"], "probability")
        self.assertEqual(sampling["counts"]["list_discovered"], 4)
        self.assertEqual(sampling["counts"]["detail_attempted"], 2)
        self.assertEqual(sampling["counts"]["not_attempted"], 2)
        self.assertEqual(sampling["bounds"]["intern_count"], [1, 3])
        markdown = render_markdown(report)
        self.assertIn("有界详情抽样", markdown)
        self.assertIn("列表发现数：4", markdown)
        self.assertIn("设计加权估计", markdown)
        self.assertIn("方差不可估", markdown)


if __name__ == "__main__":
    unittest.main()
