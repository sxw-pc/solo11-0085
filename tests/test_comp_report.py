"""Integration tests: parser attachment, lifecycle immutability, read-only
store queries and Markdown rendering for compensation explanations."""

from __future__ import annotations

import json
import sqlite3
import unittest
from fractions import Fraction

from jobtrace.comp_report import (
    compare_jobs, explain_job, explain_store, render_comparison_markdown,
    render_compensation_markdown,
)
from jobtrace.compensation import (
    analyze_feasibility, parse_compensation,
)
from jobtrace.comparison import INDETERMINATE, OPPORTUNITY, REFUSED, ROBUST
from jobtrace.models import JobObservation
from jobtrace.normalize import Experience
from jobtrace.report import (
    compare_jobs as report_compare_jobs, explain_store as report_explain_store,
)
from jobtrace.salary import parse_salary
from jobtrace.schema import SCHEMA
from jobtrace.store import Store

try:  # bs4/PyYAML are declared deps but may be absent in a slim env.
    from jobtrace.parser import parse_detail_page
    from jobtrace.selectors import DetailSelectors, ListSelectors, Selectors
except ModuleNotFoundError:  # pragma: no cover - exercised only without deps
    parse_detail_page = None
    DetailSelectors = ListSelectors = Selectors = None

A_TEXT = "20-25K·14薪，目标奖金0-6万，年包35-40万（含奖金）"
B_TEXT = "保底37-38万/年"
DETAIL_HTML = """
<html><body>
  <h1 class="t">高级后端工程师</h1>
  <div class="c">示例科技</div>
  <div class="l">上海市浦东新区</div>
  <div class="s">{salary}</div>
  <div class="e">3-5年</div>
  <div class="h">全职</div>
  <div class="d">合成岗位描述</div>
  <div class="p">2026-01-15</div>
  <div class="st">招聘中</div>
  <span class="tag">Python</span><span class="tag">分布式</span>
</body></html>
"""


def _selectors() -> Selectors:
    return Selectors(
        list=ListSelectors("", "", "", "", "", "", "", ""),
        detail=DetailSelectors(
            ".t", ".c", ".l", ".s", ".e", ".h", ".d", ".p", ".st", ".tag"),
    )

def _observation(salary_text: str) -> JobObservation:
    return JobObservation(
        site_key="jobtrace-demo", external_id="JT-X", title="工程师",
        company="示例科技", location_raw="上海", city="上海", district=None,
        salary=parse_salary(salary_text),
        experience=Experience("3-5年", 3, 5), employment_type="全职",
        description="d", tags=("Python",), detail_url="http://x/jobs/JT-X",
        compensation=parse_compensation(salary_text),
    )


@unittest.skipIf(parse_detail_page is None,
                 "beautifulsoup4 not installed in this environment")
class ParserAttachmentTests(unittest.TestCase):
    def test_detail_parser_attaches_compensation_without_extra_issues(self):
        item = type("Item", (), {"external_id": "JT-X"})()
        result = parse_detail_page(
            DETAIL_HTML.format(salary=A_TEXT), "http://x/jobs/JT-X",
            "jobtrace-demo", item, _selectors(),
        )
        self.assertTrue(result.ok)
        plan = result.observation.compensation
        self.assertIsNotNone(plan)
        # parser._text applies NFKC like every other field; raw keeps the text
        # exactly as observed at that stage (fullwidth punctuation folded).
        import unicodedata
        self.assertEqual(plan.raw, unicodedata.normalize("NFKC", A_TEXT))
        self.assertNotIn("invalid salary", " ".join(result.issues))
        feasibility = analyze_feasibility(plan)
        self.assertEqual(
            (feasibility.feasible_interval.low,
             feasibility.feasible_interval.high),
            (Fraction(350000), Fraction(400000)),
        )

    def test_compensation_field_does_not_change_content_payload_or_hash(self):
        with_plan = _observation(A_TEXT)
        without_plan = JobObservation(
            **{**with_plan.__dict__, "compensation": None},
        )
        self.assertEqual(with_plan.content_payload(), without_plan.content_payload())
        self.assertEqual(with_plan.content_hash, without_plan.content_hash)
        self.assertEqual(with_plan.tracked_values(), without_plan.tracked_values())


class StoreReadOnlyQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.conn = self.store.conn
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO runs(run_id, site_key, scenario, base_url, config_hash, "
            "started_at) VALUES(?,?,?,?,?,?)",
            ("run-1", "jobtrace-demo", "baseline", "http://x", "h", "2026-01-15T09:00:00Z"),
        )
        self._insert_job("JT-A", "岗位A", A_TEXT)
        self._insert_job("JT-B", "岗位B", B_TEXT)
        self._insert_job("JT-C", "岗位C", "月薪40K·14薪，年包30万含奖金")

    def tearDown(self):
        self.store.close()

    def _insert_job(self, external_id: str, title: str, salary_text: str) -> None:
        uid = f"job_{external_id}"
        seen = "2026-01-15T09:00:00Z"
        self.conn.execute(
            "INSERT INTO jobs(job_uid, site_key, external_id, title, company, "
            "location_raw, salary_text, salary_months, salary_negotiable, "
            "detail_url, first_seen_run_id, first_seen_at, last_seen_run_id, "
            "last_seen_at, content_hash, updated_at, status) "
            "VALUES(?,?,?,?,?,?,?,14,0,?,?,?,?,?,?,?,'open')",
            (uid, "jobtrace-demo", external_id, title, "示例科技", "上海",
             salary_text, "http://x/jobs/" + external_id, "run-1", seen,
             "run-1", seen, "hash", seen),
        )
        self.conn.execute(
            "INSERT INTO job_snapshots(job_uid, run_id, content_hash, "
            "payload_json, observed_at) VALUES(?,?,?,?,?)",
            (uid, "run-1", "hash",
             json.dumps({"salary_text": salary_text}, ensure_ascii=False),
             "2026-01-15T09:00:00Z"),
        )

    def test_explain_store_uses_snapshot_text_not_salary_min_max(self):
        entries = explain_store(self.store)
        by_id = {entry["external_id"]: entry for entry in entries}
        self.assertEqual(by_id["JT-A"]["feasible_annual_interval"],
                         "350000-400000元/年")
        self.assertEqual(by_id["JT-A"]["source_text"], A_TEXT)
        self.assertEqual(by_id["JT-B"]["feasible_annual_interval"],
                         "370000-380000元/年")
        self.assertFalse(by_id["JT-C"]["feasible"])
        conflict = by_id["JT-C"]["conflicts"][0]
        self.assertEqual(conflict["right"], "package")

    def test_compare_jobs_returns_both_contracts_with_witnesses(self):
        result = compare_jobs(self.store, "JT-A", "JT-B")
        contracts = result["contracts"]
        self.assertEqual(contracts[ROBUST]["verdict"], INDETERMINATE)
        self.assertEqual(contracts[OPPORTUNITY]["verdict"], OPPORTUNITY)
        self.assertIn("counterexample_interval", contracts[ROBUST]["witness"])
        self.assertEqual(
            contracts[OPPORTUNITY]["witness"]["a_income"], "400000 CNY")

    def test_infeasible_job_is_refused_through_store_path(self):
        result = compare_jobs(self.store, "JT-C", "JT-A")
        self.assertEqual(result["contracts"][ROBUST]["verdict"], REFUSED)
        self.assertEqual(result["contracts"][OPPORTUNITY]["verdict"], REFUSED)

    def test_unknown_job_returns_error_not_a_ranking(self):
        result = compare_jobs(self.store, "JT-A", "JT-MISSING")
        self.assertIn("error", result)

    def test_store_opened_readonly_still_serves_queries(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ro.sqlite3"
            writable = Store(db)
            writable.conn.executescript(SCHEMA)
            writable.conn.execute(
                "INSERT INTO runs(run_id, site_key, scenario, base_url, "
                "config_hash, started_at) VALUES(?,?,?,?,?,?)",
                ("run-1", "s", "baseline", "http://x", "h", "2026-01-15T09:00:00Z"),
            )
            writable.close()
            ro = Store(db, readonly=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.conn.execute("INSERT INTO runs(run_id) VALUES('x')")
                # No rows: explanation degrades explicitly instead of raising.
                self.assertEqual(explain_store(ro), [])
            finally:
                ro.close()

    def test_report_module_reexports_read_only_entry_points(self):
        self.assertIs(report_compare_jobs, compare_jobs)
        self.assertIs(report_explain_store, explain_store)


class RenderingTests(unittest.TestCase):
    def test_markdown_lists_conflicts_and_infinity_explicitly(self):
        entries = [
            explain_feasibility_entry(A_TEXT),
            explain_feasibility_entry("月薪20K·13薪，提成上不封顶"),
            explain_feasibility_entry("月薪40K·14薪，年包30万含奖金"),
        ]
        markdown = render_compensation_markdown(entries)
        self.assertIn("350000-400000元/年", markdown)
        self.assertIn("∞", markdown)
        self.assertIn("无解", markdown)
        self.assertIn("component_sum∩package", markdown)

    def test_comparison_markdown_shows_two_contracts_and_witness(self):
        result = {
            "a": "JT-A", "b": "JT-B",
            "sources": {"a": A_TEXT, "b": B_TEXT},
            "contracts": {
                ROBUST: {"verdict": INDETERMINATE, "reason": "r1",
                         "witness": {"counterexample_interval": "A=350000 与 B=380000"}},
                OPPORTUNITY: {"verdict": OPPORTUNITY, "reason": "r2",
                              "witness": {"a_income": "400000 CNY"}},
            },
        }
        markdown = render_comparison_markdown(result)
        self.assertIn("稳健优势", markdown)
        self.assertIn("机会优势", markdown)
        self.assertIn("400000 CNY", markdown)


def explain_feasibility_entry(text: str) -> dict:
    from jobtrace.comparison import explain_feasibility
    return {
        "external_id": "JT-" + text[:2], "source_text": text,
        **explain_feasibility(analyze_feasibility(parse_compensation(text))),
    }


if __name__ == "__main__":
    unittest.main()
