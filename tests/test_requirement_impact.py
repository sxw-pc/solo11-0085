"""Restricted requirement parsing, eligibility impact, and audit contracts.

Covers the distributive equivalence, tighten/relax witnesses, negation scope,
duplicate/order insensitivity, unknown grammar, missing descriptions, both
audit contracts, and the read-only report built from stored observations.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jobtrace.config import DEFAULT_SELECTORS, load_selectors
from jobtrace.models import JobObservation
from jobtrace.normalize import Experience, normalize_tags
from jobtrace.requirement_evidence import recompute_impact_evidence, requirement_report
from jobtrace.requirements import (
    Rules,
    audit_revision,
    impact_audit,
    parse_requirements,
    text_audit,
)
from jobtrace.salary import parse_salary
from jobtrace.store import Store

AND_DISTRIBUTED = "Python 且 (SQL 或 Airflow)"
OR_DISTRIBUTED = "(Python 且 SQL) 或 (Python 且 Airflow)"
PY_SQL = "Python 且 SQL"
FIXED = "2026-01-15T09:00:00Z"
def _parse(text):
    return parse_requirements(text)


def _observation(description, external_id="JT-0001"):
    return JobObservation(
        site_key="site-a", external_id=external_id, title="合成岗位", company="示例公司",
        location_raw="上海 · 徐汇区", city="上海", district="徐汇区", salary=parse_salary("20-30K"),
        experience=Experience(text="3-5年", min_years=3, max_years=5), employment_type="全职",
        description=description, tags=normalize_tags(["Python"]), detail_url="http://127.0.0.1/jobs/x",
    )


class SyntaxTests(unittest.TestCase):
    def test_distributive_forms_are_equivalent(self):
        result = impact_audit(AND_DISTRIBUTED, OR_DISTRIBUTED)
        self.assertEqual(result["status"], "no_substantive_change")
        self.assertTrue(result["old_implies_new"] and result["new_implies_old"])
        self.assertTrue(result["equivalent"])
    def test_word_sets_do_not_decide_tightening(self):
        result = impact_audit(AND_DISTRIBUTED, PY_SQL)
        self.assertEqual(result["status"], "eligibility_changed")
        self.assertEqual(result["direction"], "tighten")
        self.assertFalse(result["old_implies_new"])
        self.assertTrue(result["new_implies_old"])
        self.assertEqual(result["lost"]["skills"], ["Airflow", "Python"])
    def test_witness_points_back_to_old_clause(self):
        result = impact_audit(AND_DISTRIBUTED, PY_SQL)
        refs = result["lost"]["clauses"]
        self.assertTrue(refs)
        for ref in refs:
            self.assertEqual(ref["clause_text"], AND_DISTRIBUTED)
            self.assertEqual(ref["clause_span"], {"start": 0, "end": len(AND_DISTRIBUTED)})
            span = ref["atom_span"]
            self.assertEqual(AND_DISTRIBUTED[span["start"]:span["end"]], ref["skill"])
        self.assertEqual(result["lost"]["other_missing_skills"], ["SQL"])
    def test_relaxation_has_gained_witness(self):
        result = impact_audit(PY_SQL, AND_DISTRIBUTED)
        self.assertEqual(result["direction"], "relax")
        self.assertEqual(result["gained"]["skills"], ["Airflow", "Python"])
        self.assertEqual(result["gained"]["other_missing_skills"], ["SQL"])
    def test_negation_scope_is_respected(self):
        negative = _parse("Python 且 不要求 SQL")
        self.assertTrue(negative.decidable)
        self.assertTrue(negative.as_dict()["render"].count("不要求"))
        self.assertEqual(impact_audit("Python 且 不要求 SQL", "Python 且 SQL")["direction"], "tighten")
        self.assertEqual(impact_audit("Python 且 不要求 SQL", "Python")["status"], "no_substantive_change")
    def test_duplicates_and_clause_order_do_not_change_eligibility(self):
        reordered = "Python 且 SQL 或 Python 且 Airflow"
        duplicate = "Python 且 Python 且 (SQL 或 Airflow)"
        for text in (duplicate, reordered):
            parse = _parse(text)
            self.assertEqual(parse.status, "OK", parse.reason)
            result = impact_audit(text, AND_DISTRIBUTED)
            self.assertEqual(result["status"], "no_substantive_change", text)
    def test_clause_order_and_split_clauses(self):
        result = impact_audit("Python 且 SQL。Airflow 或 Spark", "Airflow 或 Spark。SQL 且 Python")
        self.assertEqual(result["status"], "no_substantive_change")
        self.assertEqual(_parse("Python。SQL。").skills, ("Python", "SQL"))
    def test_unknown_natural_language_is_undecidable(self):
        for text in ("负责合成业务并复盘", "必须掌握 Python 的深厚经验", "Python 且热爱团队"):
            parse = _parse(text)
            self.assertEqual(parse.status, "UNKNOWN", text)
            self.assertIsNone(parse.formula)
            result = impact_audit(text, "Python")
            self.assertEqual(result["status"], "undecidable")
            self.assertIsNone(result["direction"])
            self.assertIn("no empty-formula assumption", result["reason"])
    def test_missing_detail_is_not_an_empty_formula(self):
        for old, new in ((None, "Python"), ("Python", None), (None, None)):
            result = impact_audit(old, new)
            self.assertEqual(result["status"], "undecidable")
            self.assertIsNone(result["equivalent"])
        self.assertEqual(_parse(None).status, "MISSING")
        self.assertEqual(_parse("   ").status, "MISSING")
    def test_explicit_no_requirement_is_true(self):
        parse = _parse("无技能要求")
        self.assertEqual(parse.status, "OK")
        self.assertEqual(parse.as_dict()["render"], "TRUE")
        self.assertEqual(impact_audit("无技能要求", "Python")["direction"], "tighten")
        self.assertEqual(impact_audit("无技能要求", "不要求 Python")["status"], "no_substantive_change")
    def test_bounded_domain_marks_oversized_as_undecidable(self):
        names = [f"Skill{i}".lower() for i in range(20)]
        widened = Rules(version="oversized", synonyms=tuple((n, n.title()) for n in names))
        result = impact_audit(" 和 ".join(names), names[0], widened)
        self.assertEqual(result["status"], "undecidable")
        self.assertIn("bounded", result["reason"])


class ContractTests(unittest.TestCase):
    def test_text_audit_reports_revision_for_equivalent_rewrite(self):
        text = text_audit(AND_DISTRIBUTED, OR_DISTRIBUTED)
        self.assertEqual(text["status"], "text_revised")
        self.assertTrue(text["description_changed"])
        self.assertNotEqual(text["old_sha256"], text["new_sha256"])
    def test_impact_contract_reports_no_change_for_equivalent_rewrite(self):
        audit = audit_revision(AND_DISTRIBUTED, OR_DISTRIBUTED)
        self.assertEqual(audit["text_audit"]["status"], "text_revised")
        self.assertEqual(audit["impact"]["status"], "no_substantive_change")
        self.assertEqual(audit["action"], "text_revision")
    def test_both_contracts_report_tightening_with_lost_witness(self):
        audit = audit_revision(AND_DISTRIBUTED, PY_SQL)
        self.assertEqual(audit["text_audit"]["status"], "text_revised")
        self.assertEqual(audit["impact"]["status"], "eligibility_changed")
        self.assertEqual(audit["impact"]["direction"], "tighten")
        self.assertIn("Airflow", audit["impact"]["lost"]["skills"])
        self.assertNotIn("SQL", audit["impact"]["lost"]["skills"])
    def test_equivalent_text_difference_is_still_a_text_revision(self):
        audit = audit_revision("Python 且 SQL", "SQL 且 Python")
        self.assertEqual(audit["text_audit"]["status"], "text_revised")
        self.assertEqual(audit["impact"]["status"], "no_substantive_change")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "evidence.sqlite3")
        self.addCleanup(self.store.close)
    def _apply(self, run_id, description):
        from jobtrace.lifecycle import LifecycleEngine
        return LifecycleEngine(self.store).apply( run_id=run_id, site_key="site-a", observations=[_observation(description)], complete=False, now=FIXED, close_after_missing_runs=2, )
    def test_lifecycle_records_both_contracts_without_content_hash_noise(self):
        self._apply("run-1", AND_DISTRIBUTED)
        result = self._apply("run-2", OR_DISTRIBUTED)
        self.assertEqual(result.changed_events, 0)
        self.assertEqual(result.requirement_text_revisions, 1)
        self.assertEqual(result.requirement_impact_changes, 0)
        rows = self.store.list_requirement_evidence(contract="text_audit")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["change_kind"], "text_revised")
        impact = self.store.list_requirement_evidence(contract="impact")[0]
        self.assertEqual(impact["change_kind"], "no_substantive_change")
        self.assertEqual(impact["eligibility_changed"], 0)
    def test_lifecycle_flags_tightening_impact(self):
        self._apply("run-1", AND_DISTRIBUTED)
        result = self._apply("run-2", PY_SQL)
        self.assertEqual(result.requirement_impact_changes, 1)
        impact = self.store.list_requirement_evidence(contract="impact")[0]
        payload = json.loads(impact["evidence_json"])
        self.assertEqual(payload["direction"], "tighten")
        self.assertEqual(payload["lost"]["skills"], ["Airflow", "Python"])
    def test_rules_version_bump_recomputes_derived_only(self):
        self._apply("run-1", AND_DISTRIBUTED)
        self._apply("run-2", PY_SQL)
        job = self.store.list_jobs()[0]
        before = (job["description"], job["content_hash"])
        from jobtrace.requirements import DEFAULT_RULES
        bumped = Rules(version="req-rules-v2", synonyms=DEFAULT_RULES.synonyms)
        count = recompute_impact_evidence(self.store, bumped, now=FIXED)
        self.assertEqual(count, 1)
        job = self.store.list_jobs()[0]
        self.assertEqual((job["description"], job["content_hash"]), before)
        report = requirement_report(self.store, job_uid=job["job_uid"], rules_version="req-rules-v2")
        self.assertEqual(report["stored_rules_version"], "req-rules-v2")
        self.assertEqual(len(report["evidence"]), 2)
        self.assertTrue(all(item["rules_version"] == "req-rules-v2" for item in report["evidence"]))
    def test_report_is_read_only_and_keeps_source_run(self):
        self._apply("run-1", AND_DISTRIBUTED)
        self._apply("run-2", PY_SQL)
        job = self.store.list_jobs()[0]
        report = requirement_report(self.store, job_uid=job["job_uid"])
        evidence = report["evidence"]
        self.assertEqual({item["contract"] for item in evidence}, {"text_audit", "impact"})
        self.assertTrue(all(item["old_run_id"] and item["run_id"] for item in evidence))
        readonly = Store(Path(self.directory.name) / "evidence.sqlite3", readonly=True)
        self.addCleanup(readonly.close)
        self.assertEqual(requirement_report(readonly, job_uid=job["job_uid"])["evidence"], evidence)


class ParserIntegrationTests(unittest.TestCase):
    def test_detail_parser_attaches_requirement_parse(self):
        from jobtrace.mock_site import create_app
        from jobtrace.parser import parse_detail_page, parse_list_page
        app = create_app("baseline")
        client = app.test_client()
        listing = client.get("/jobs?page=1")
        item = parse_list_page(listing.get_data(as_text=True), "http://127.0.0.1/jobs", load_selectors(DEFAULT_SELECTORS)).items[0]
        detail = client.get(f"/jobs/{item.external_id}")
        result = parse_detail_page(detail.get_data(as_text=True), "http://127.0.0.1/jobs/x", "site-a", item, load_selectors(DEFAULT_SELECTORS))
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.observation.requirement)
        self.assertEqual(result.observation.requirement.description_sha256, _parse(result.observation.description).description_sha256)


if __name__ == "__main__":
    unittest.main()
