"""合成履历 × 受限岗位要求的资格裁决测试（全离线、无个人信息、纯标准库）。"""

from __future__ import annotations

import json
import unittest
from fractions import Fraction

from jobtrace.models import JobObservation
from jobtrace.normalize import normalize_experience
from jobtrace.report import render_qualification_markdown
from jobtrace.salary import SalaryInfo
from jobtrace.skill_eval import evaluate, evaluate_query
from jobtrace.skill_exp import (
    RequirementError, entry_from_dict, make_entry, parse_requirement,
    requirement_from_dict,
)

REQ = "Python>=18 AND SQL>=12"


def resume_r():
    """R：18 个月连续全职，每月同时使用 Python 与 SQL。"""
    return [make_entry("2024-01-01", "2025-07-01", ("Python", "SQL"), 1, "R-全职")]


def resume_s():
    """S：跨月交错技能；Python/SQL 个体上界各 3 个月，仅 2024-03 为共享月。"""
    return [
        make_entry("2024-01-01", "2024-02-01", ("Python",), 1, "S1"),
        make_entry("2024-02-01", "2024-03-01", ("SQL",), 1, "S2"),
        make_entry("2024-03-01", "2024-04-01", ("Python", "SQL"), 1, "S3-共享"),
        make_entry("2024-04-01", "2024-05-01", ("Python",), 1, "S4"),
        make_entry("2024-05-01", "2024-06-01", ("SQL",), 1, "S5"),
    ]


class RequirementParserTests(unittest.TestCase):
    def test_and_conjunction_both_syntaxes(self):
        req = parse_requirement(REQ)
        self.assertEqual([(c.label, c.min_months) for c in req.clauses],
                         [("Python", 18), ("SQL", 12)])
        cn = parse_requirement("Python至少18个月且SQL至少12个月")
        self.assertEqual([(c.key, c.min_months) for c in cn.clauses],
                         [("python", 18), ("sql", 12)])
        three = parse_requirement("Python>=18 AND SQL>=12 且 Go>=6")
        self.assertEqual(len(three.clauses), 3)

    def test_rejects_anything_outside_restricted_grammar(self):
        for text in ("", "Python>=18 OR SQL>=12", "Python>=18, SQL>=12", "3-5年",
                     "应届", "不限", "Python>=0", "Python>=18 AND Python>=6", "Python"):
            with self.assertRaises(RequirementError, msg=text):
                parse_requirement(text)

    def test_legacy_experience_text_stays_with_normalize_experience(self):
        self.assertEqual((normalize_experience("3-5年").min_years,
                          normalize_experience("3-5年").max_years), (3, 5))
        self.assertEqual(normalize_experience("应届").text, "应届")
        self.assertIsNone(normalize_experience("不限").min_years)
        for text in ("3-5年", "应届", "不限"):
            with self.assertRaises(RequirementError):
                parse_requirement(text)


class ExposureContractTests(unittest.TestCase):
    def test_resume_r_satisfies_18_and_12_with_same_months(self):
        report = evaluate(resume_r(), REQ, contract="exposure")
        self.assertEqual(report.verdict, "qualified")
        self.assertEqual({c.label: c.counted for c in report.clauses},
                         {"Python": "18", "SQL": "18"})
        python = next(c for c in report.clauses if c.label == "Python")
        self.assertEqual(len(python.evidence), 18)
        self.assertEqual(python.evidence[0]["month"], "2024-01")
        self.assertEqual(python.evidence[0]["segments"], ["seg-1"])

    def test_resume_s_exposure_passes_but_that_is_not_dedicated(self):
        report = evaluate(resume_s(), "Python>=3 AND SQL>=3", contract="exposure")
        self.assertEqual(report.verdict, "qualified")

    def test_half_open_interval_and_mid_month_proration(self):
        entries = [make_entry("2024-01-15", "2024-03-10", ("Python",), 1, "跨界")]
        clause = evaluate(entries, "Python>=3", contract="exposure").clauses[0]
        self.assertEqual(clause.status, "satisfied")
        amounts = {item["month"]: item["amount"] for item in clause.evidence}
        self.assertEqual(amounts, {"2024-01": str(Fraction(17, 31)), "2024-02": "1",
                                   "2024-03": str(Fraction(9, 31))})
        one = evaluate([make_entry("2024-01-01", "2024-02-01", ("Python",))],
                       "Python>=1", contract="exposure")
        self.assertEqual([i["month"] for i in one.clauses[0].evidence], ["2024-01"])

    def test_unknown_skill_is_insufficient_evidence_not_zero_years(self):
        report = evaluate(resume_r(), "Go>=6 AND Python>=1", contract="exposure")
        self.assertEqual(report.verdict, "insufficient_evidence")
        go = next(c for c in report.clauses if c.label == "Go")
        self.assertEqual((go.status, go.evidence), ("insufficient_evidence", ()))
        python = next(c for c in report.clauses if c.label == "Python")
        self.assertEqual(python.status, "satisfied")


class DedicatedContractTests(unittest.TestCase):
    def test_resume_r_fails_joint_capacity_30_over_18(self):
        report = evaluate(resume_r(), REQ, contract="dedicated")
        self.assertEqual(report.verdict, "unqualified")
        witness = report.witness
        self.assertEqual((witness["demand_total"], witness["allocated_total"],
                          witness["deficit"]), ("30", "18", "12"))
        self.assertEqual({c.label: c.counted for c in report.clauses},
                         {"Python": "18", "SQL": "18"})  # 个体上界够，联合不可行

    def test_resume_s_outputs_capacity_conflict_witness(self):
        report = evaluate(resume_s(), "Python>=3 AND SQL>=3", contract="dedicated")
        self.assertEqual(report.verdict, "unqualified")
        witness = report.witness
        self.assertEqual((witness["demand_total"], witness["allocated_total"],
                          witness["deficit"]), ("6", "5", "1"))
        self.assertEqual(witness["shared_months"], ["2024-03"])
        self.assertEqual(len(witness["saturated_months"]), 5)
        for clause in report.clauses:
            self.assertEqual((clause.status, clause.counted), ("unmet", "3"))

    def test_global_allocation_not_requirement_order_greedy(self):
        entries = [make_entry("2024-01-01", "2024-02-01", ("Python", "SQL"), 1, "共享"),
                   make_entry("2024-02-01", "2024-03-01", ("Python",), 1, "仅Python")]
        report = evaluate(entries, "Python>=1 AND SQL>=1", contract="dedicated")
        self.assertEqual(report.verdict, "qualified")
        sql = next(c for c in report.clauses if c.label == "SQL")
        self.assertEqual([i["month"] for i in sql.evidence], ["2024-01"])

    def test_full_time_plus_half_part_time_uses_actual_ratios(self):
        entries = [make_entry("2024-01-01", "2024-03-01", ("Python",), 1, "全职"),
                   make_entry("2024-01-01", "2024-03-01", ("SQL",), "0.5", "兼职")]
        capped = [i for i in evaluate(entries, REQ, contract="exposure").issues
                  if i["code"] == "month_capped"]
        self.assertEqual(len(capped), 2)  # 每月申报 1.5，按上限 1 计
        ok = evaluate(entries, "Python>=1 AND SQL>=1", contract="dedicated")
        self.assertEqual(ok.verdict, "qualified")
        sql = next(c for c in ok.clauses if c.label == "SQL")
        self.assertEqual(sum(Fraction(i["amount"]) for i in sql.evidence), 1)
        too_much = evaluate(entries, "Python>=2 AND SQL>=2", contract="dedicated")
        self.assertEqual((too_much.verdict, too_much.witness["deficit"]),
                         ("unqualified", "2"))

    def test_prorated_months_feed_dedicated_capacity(self):
        entries = [make_entry("2024-01-15", "2024-03-10", ("Python",), 1, "跨界")]
        report = evaluate(entries, "Python>=2", contract="dedicated")
        self.assertEqual(report.verdict, "unqualified")
        self.assertEqual(report.witness["allocated_total"], str(Fraction(57, 31)))


class HygieneTests(unittest.TestCase):
    def test_inverted_empty_and_invalid_ratio_entries_dropped(self):
        entries = [make_entry("2024-03-01", "2024-01-01", ("Python",), 1, "倒置"),
                   make_entry("2024-01-01", "2024-02-01", (), 1, "空技能"),
                   make_entry("2024-01-01", "2024-02-01", ("Python",), "1.5", "超界"),
                   make_entry("2024-01-01", "2024-02-01", ("Python",), 0, "零投入"),
                   make_entry("2024-01-01", "2024-02-01", ("Python",), 1, "有效")]
        report = evaluate(entries, "Python>=1", contract="exposure")
        self.assertEqual(report.verdict, "qualified")
        self.assertEqual([i["code"] for i in report.issues],
                         ["inverted_dates", "empty_skills", "invalid_ratio", "invalid_ratio"])
        self.assertEqual(report.resume["segment_count"], 1)
        self.assertEqual(len(report.clauses[0].evidence), 1)  # 倒置段不产出月份

    def test_duplicate_entries_deduped_not_doubled(self):
        entries = [make_entry("2024-01-01", "2024-02-01", ("Python",), 1, "甲"),
                   make_entry("2024-01-01", "2024-02-01", ("Python",), 1, "乙")]
        report = evaluate(entries, "Python>=2", contract="exposure")
        self.assertEqual((report.verdict, report.clauses[0].counted), ("unqualified", "1"))
        self.assertEqual(report.issues[0]["code"], "duplicate_entry")
        dedicated = evaluate(entries, "Python>=2", contract="dedicated")
        self.assertEqual(dedicated.witness["allocated_total"], "1")

    def test_empty_resume_is_insufficient_evidence(self):
        report = evaluate([], "Python>=1", contract="dedicated")
        self.assertEqual((report.verdict, report.clauses[0].status),
                         ("insufficient_evidence", "insufficient_evidence"))


class QueryIoTests(unittest.TestCase):
    def test_evaluate_query_from_local_synthetic_json(self):
        payload = {"requirement": REQ, "contract": "dedicated",
                   "resume": [{"start": "2024-01-01", "end": "2025-07-01",
                               "skills": ["Python", "SQL"], "ratio": 1, "label": "R"}]}
        report = evaluate_query(payload)
        self.assertEqual(report.verdict, "unqualified")
        encoded = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertIn('"deficit": "12"', encoded)

    def test_requirement_and_entry_dict_round_trip(self):
        req = parse_requirement(REQ)
        again = requirement_from_dict(req.to_dict())
        self.assertEqual([(c.key, c.min_months) for c in again.clauses],
                         [("python", 18), ("sql", 12)])
        entry = make_entry("2024-01-01", "2024-02-01", ("Python",), "0.5", "段")
        self.assertEqual(entry_from_dict(entry.to_dict()), entry)
        query = evaluate_query({"requirement": req.to_dict(), "contract": "exposure",
                                "resume": [entry.to_dict()]})
        self.assertEqual(query.requirement.clauses[0].min_months, 18)

    def test_contract_must_be_chosen_explicitly(self):
        with self.assertRaises(ValueError):
            evaluate(resume_r(), REQ, contract="sometimes")
        with self.assertRaises(ValueError):
            evaluate_query({"requirement": REQ, "resume": []})

    def test_markdown_renders_audit_witness_and_hygiene(self):
        conflict = evaluate(resume_s(), "Python>=3 AND SQL>=3", contract="dedicated")
        text = render_qualification_markdown(conflict)
        for token in ("容量冲突见证", "2024-03", "不合格", "专属投入", "seg-3"):
            self.assertIn(token, text)
        dup = evaluate([make_entry("2024-01-01", "2024-02-01", ("Python",), 1)] * 2,
                       "Python>=1", contract="exposure")
        self.assertIn("履历处置说明", render_qualification_markdown(dup))
        unknown = evaluate(resume_r(), "Go>=6", contract="exposure")
        self.assertIn("证据不足", render_qualification_markdown(unknown))


class FingerprintBoundaryTests(unittest.TestCase):
    def test_site_content_payload_not_extended_by_query_data(self):
        obs = JobObservation(
            site_key="synthetic", external_id="J1", title="t", company="c",
            location_raw="杭州", city="杭州", district=None, salary=SalaryInfo(),
            experience=normalize_experience("3-5年"), employment_type="全职",
            description="d", tags=(), detail_url="http://127.0.0.1/jobs/J1")
        self.assertEqual(set(obs.content_payload()), {
            "external_id", "title", "company", "location_raw", "city", "district",
            "salary_min", "salary_max", "salary_text", "salary_months", "salary_period",
            "salary_negotiable", "experience_text", "experience_min", "experience_max",
            "employment_type", "description", "tags", "detail_url", "posted_at",
            "source_status"})
        self.assertEqual(obs.content_hash, obs.content_hash)


if __name__ == "__main__":
    unittest.main()
