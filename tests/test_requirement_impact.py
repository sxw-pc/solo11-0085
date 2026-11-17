from __future__ import annotations
import json
import unittest
from jobtrace.impact import (
    application_impact_contract, compare_requirements, describe_comparison,
    requirement_report, text_audit_contract,
)
from jobtrace.lifecycle import LifecycleEngine
from jobtrace.models import JobObservation, ListItem
from jobtrace.normalize import Experience
from jobtrace.parser import parse_detail_page
from jobtrace.requirements import RULESET_VERSION, Ruleset, parse_requirements, render
from jobtrace.salary import parse_salary
from jobtrace.store import Store
A = "Python 且 (SQL 或 Airflow)"
B = "(Python 且 SQL) 或 (Python 且 Airflow)"
TIGHT = "Python 且 SQL"
def _sets(witnesses) -> list[set[str]]: return [set(item.skills) for item in witnesses]
class FormulaEquivalenceTests(unittest.TestCase):
    def test_distributive_and_reassociation_rewrites_are_equivalent(self):
        c = compare_requirements(A, B)
        self.assertTrue(c.decidable)
        self.assertIs(c.old_implies_new and c.new_implies_old, True)
        self.assertFalse(c.qualification_set_changed)
        self.assertEqual(c.gained + c.lost, ())
        self.assertIn("Python", render(parse_requirements(A).root))
        reassoc = compare_requirements("(Python 或 SQL) 或 (Airflow 或 Kafka)",
                                       "Kafka 或 ((Airflow 或 SQL) 或 Python)")
        self.assertTrue(reassoc.old_implies_new and reassoc.new_implies_old)
    def test_word_set_alone_cannot_decide_equivalence(self):
        c = compare_requirements("Python 或 SQL", TIGHT)  # same skills, different set
        self.assertFalse(c.old_implies_new)
        self.assertTrue(c.new_implies_old)
    def test_duplicates_reordering_and_implicit_forms_keep_one_set(self):
        self.assertFalse(compare_requirements(
            "Python 且 SQL；Python 且 SQL", TIGHT).qualification_set_changed)
        self.assertFalse(compare_requirements("SQL 且 Python", TIGHT).qualification_set_changed)
        self.assertFalse(compare_requirements(
            "Airflow；Python；SQL", "Python 且 SQL 且 Airflow").qualification_set_changed)
        for text in ("Python、SQL", "Python SQL", "Python，SQL"):
            self.assertFalse(compare_requirements(text, TIGHT).qualification_set_changed, text)
        parsed = parse_requirements("Python 且 SQL；Python 且 SQL")
        self.assertEqual(len(parsed.clauses), 2)
        self.assertNotEqual(parsed.clauses[0].start, parsed.clauses[1].start)
        self.assertEqual(parse_requirements("k8s 且 TS").skills, ("Kubernetes", "TypeScript"))
    def test_tightening_relaxation_witnesses_and_minimality(self):
        c = compare_requirements(A, TIGHT)
        self.assertFalse(c.old_implies_new)
        self.assertTrue(c.new_implies_old)
        self.assertIn({"Python", "Airflow"}, _sets(c.lost))
        for witness in c.lost:
            self.assertNotIn("SQL", witness.skills)
            self.assertTrue(witness.clause_indices and witness.spans)
        witness = next(item for item in c.lost if set(item.skills) == {"Python", "Airflow"})
        self.assertEqual(witness.clause_indices, (0,))
        self.assertEqual({A[s:e] for s, e in witness.spans}, {"Python", "Airflow"})
        relaxed = compare_requirements(TIGHT, A)
        self.assertTrue(relaxed.old_implies_new)
        self.assertFalse(relaxed.new_implies_old)
        self.assertIn({"Python", "Airflow"}, _sets(relaxed.gained))
        self.assertEqual(relaxed.lost, ())
        minimal = compare_requirements("Python", "Python 且 SQL 且 Airflow")
        self.assertTrue(all(len(item.skills) == 1 for item in minimal.lost))
        branches = compare_requirements("Python 或 SQL", TIGHT)
        self.assertEqual(sorted(_sets(branches.lost), key=lambda item: sorted(item)),
                         [{"Python"}, {"SQL"}])
    def test_negation_scope_de_morgan_and_double_negation(self):
        self.assertTrue(parse_requirements("Python 且 不要求 Kafka").decidable)
        c = compare_requirements("Python 且 不要求 Kafka", "Python")
        self.assertTrue(c.old_implies_new)
        self.assertFalse(c.new_implies_old)
        self.assertIn({"Python", "Kafka"}, _sets(c.gained))
        source = "Python 且 不要求 Kafka"
        self.assertTrue(any("Kafka" in source[s:e] for s, e in c.gained[0].spans))
        morgan = compare_requirements("不要求 (Python 或 SQL)", "不要求 Python 且 不要求 SQL")
        self.assertTrue(morgan.old_implies_new and morgan.new_implies_old)
        diverge = compare_requirements("Python 或 SQL", "不要求 Python 且 不要求 SQL")
        self.assertTrue(diverge.qualification_set_changed)
        self.assertFalse(diverge.old_implies_new or diverge.new_implies_old)
        self.assertFalse(compare_requirements(
            "不要求 不要求 Python", "Python").qualification_set_changed)
class GrammarAndContractTests(unittest.TestCase):
    def test_unknown_or_partial_prose_is_undecidable_never_empty(self):
        parsed = parse_requirements("需要熟悉敏捷协作与业务复盘")
        self.assertFalse(parsed.decidable)
        self.assertTrue(parsed.unknown_spans)
        self.assertNotIn("no requirement", parsed.reason)
        self.assertFalse(parse_requirements("Python 且 熟悉云平台").decidable)
    def test_mixed_operators_broken_syntax_and_missing_sides(self):
        self.assertFalse(parse_requirements("(Python 或 SQL 且 Airflow)").decidable)
        for text in ("Python 且 (SQL", "Python 且", "且 Python", "(Python)) 且 SQL"):
            self.assertFalse(parse_requirements(text).decidable, text)
        for old, new in ((None, TIGHT), ("", "Python"), ("需要熟悉敏捷流程", TIGHT),
                         (TIGHT, "负责虚构业务的综合工作")):
            c = compare_requirements(old, new)
            self.assertFalse(c.decidable)
            self.assertIsNone(c.old_implies_new)
            self.assertIsNone(c.new_implies_old)
            self.assertFalse(c.qualification_set_changed)
            self.assertEqual(c.gained + c.lost, ())
        parsed = parse_requirements("岗位要求：Python 且 SQL")
        self.assertTrue(parsed.decidable)
        self.assertEqual(parsed.skills, ("Python", "SQL"))
    def test_two_contracts_split_text_revision_from_qualification_change(self):
        self.assertTrue(text_audit_contract(A, B).changed)
        equivalent = application_impact_contract(A, B)
        self.assertFalse(equivalent.changed)
        self.assertEqual(equivalent.kind, "equivalent_rewrite")
        self.assertTrue(text_audit_contract(A, TIGHT).changed)
        tightened = application_impact_contract(A, TIGHT)
        self.assertTrue(tightened.changed and tightened.kind == "qualification_change")
        self.assertIn({"Python", "Airflow"}, _sets(tightened.comparison.lost))
        self.assertFalse(text_audit_contract(TIGHT, TIGHT).changed)
        self.assertFalse(application_impact_contract(TIGHT, TIGHT).changed)
        undecidable = application_impact_contract(None, TIGHT)
        self.assertFalse(undecidable.changed)
        self.assertEqual(undecidable.kind, "undecidable")
        payload = json.dumps(application_impact_contract(A, TIGHT).as_dict(),
                             ensure_ascii=False)
        self.assertIn("qualification_change", payload)
    def test_ruleset_version_only_recomputes_derived_evidence(self):
        ruleset = Ruleset("rules-test.2", ("Python", "SQL", "Airflow", "Rust"),
                          {"Python": ("python", "py")})
        raw = "py 且 SQL"
        first, second = parse_requirements(raw, Ruleset.default()), parse_requirements(raw, ruleset)
        self.assertFalse(first.decidable)
        self.assertTrue(second.decidable)
        self.assertEqual(second.skills, ("Python", "SQL"))
        self.assertEqual(first.text, second.text)  # raw text never rewritten
        c = compare_requirements("Rust", "Rust 且 SQL", ruleset)
        self.assertTrue(c.qualification_set_changed)
        self.assertEqual(c.ruleset_version, "rules-test.2")
        self.assertIn({"Rust"}, _sets(c.lost))
        self.assertEqual(parse_requirements("Python").ruleset_version, RULESET_VERSION)
    def test_witness_order_is_stable_and_not_first_match_driven(self):
        args = ("Python 或 SQL 或 Airflow 或 Kafka", TIGHT)
        first, second = compare_requirements(*args), compare_requirements(*args)
        self.assertEqual([w.as_dict() for w in first.lost], [w.as_dict() for w in second.lost])
        self.assertIn({"SQL"}, _sets(compare_requirements("Python 或 SQL", TIGHT).lost))
        reverse = compare_requirements(TIGHT, "Python 或 SQL")
        self.assertEqual(reverse.lost, ())
        self.assertIn({"SQL"}, _sets(reverse.gained))
def _observation(description: str) -> JobObservation:
    return JobObservation(
        site_key="site-a", external_id="JT-1", title="工程师", company="虚构公司",
        location_raw="上海", city="上海", district=None, salary=parse_salary(""),
        experience=Experience(None, None, None), employment_type="全职",
        description=description, tags=(), detail_url="http://127.0.0.1/jobs/JT-1",
        requirement_parse=parse_requirements(description))
class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.engine = LifecycleEngine(self.store)
    def tearDown(self) -> None: self.store.close()
    def _apply(self, run_id: str, description: str, day: int) -> object:
        return self.engine.apply(run_id=run_id, site_key="site-a",
            observations=[_observation(description)], complete=True,
            now=f"2026-01-{day:02d}T00:00:00Z", close_after_missing_runs=2)
    def _sequence(self, descriptions: list[str]) -> None:
        for index, description in enumerate(descriptions, start=1):
            self._apply(f"run-{index}", description, index)
    def test_lifecycle_events_ignore_description_but_impact_is_recorded(self):
        first = self._apply("run-1", A, 1)
        self.assertEqual(first.changed_events, 0)
        equivalent = self._apply("run-2", B, 2)
        self.assertEqual(self.store.event_counts("run-2")["CHANGED"], 0)
        self.assertTrue(equivalent.requirement_impacts[0]["text_revised"])
        self.assertFalse(
            equivalent.requirement_impacts[0]["impact"]["qualification_set_changed"])
        tightened = self._apply("run-3", TIGHT, 3)
        self.assertEqual(tightened.changed_events, 0)
        self.assertEqual(self.store.event_counts("run-3")["CHANGED"], 0)
        impact = tightened.requirement_impacts[0]["impact"]
        self.assertTrue(impact["qualification_set_changed"])
        self.assertIn(["Airflow", "Python"], [w["skills"] for w in impact["lost"]])
    def test_content_hash_stays_raw_and_undecidable_change_is_not_faked(self):
        first, equiv = _observation(A), _observation(B)
        self.assertNotEqual(first.content_hash, equiv.content_hash)
        self.assertEqual(first.content_payload()["description"], A)
        self.assertNotIn("requirement_parse", first.content_payload())
        self._apply("run-1", "负责虚构业务，完成综合工作。", 1)
        result = self._apply("run-2", "负责另一项虚构业务。", 2)
        impact = result.requirement_impacts[0]["impact"]
        self.assertFalse(impact["decidable"] or impact["qualification_set_changed"])
    def test_readonly_report_contracts_runs_replay_and_parser_attachment(self):
        self._sequence([A, B, TIGHT])
        report = requirement_report(self.store, site_key="site-a")
        self.assertEqual((report["text_revisions"], report["equivalent_rewrites"],
                          report["qualification_changes"]), (2, 1, 1))
        entry = report["revisions"][1]
        self.assertIn(("Airflow", "Python"),
                      {tuple(w["skills"]) for w in entry["impact"]["lost"]})
        self.assertTrue(entry["text_audit"]["changed"]
                        and entry["application_impact"]["changed"])
        lines = describe_comparison(report["revisions"])
        self.assertTrue(any("qualification change" in line for line in lines))
        self.assertEqual(report["revisions"][0]["old_run_id"], "run-1")
        self.assertEqual(report["revisions"][0]["old_description"], A)
        custom = Ruleset("rules-test.9", ("Python", "SQL"), {"Python": ("py",)})
        fresh = Store(":memory:")
        LifecycleEngine(fresh).apply(run_id="r", site_key="site-a",
            observations=[_observation("py 且 SQL")], complete=True,
            now="2026-01-01T00:00:00Z", close_after_missing_runs=2)
        self.assertEqual(requirement_report(fresh)["revisions"], [])
        self.assertEqual(requirement_report(fresh, ruleset=custom)["ruleset_version"],
                         "rules-test.9")
        self.assertEqual(len(fresh.list_events(limit=100)), 1)  # NEW only
        fresh.close()
        from jobtrace.config import DEFAULT_SELECTORS, load_selectors
        html = ("<html><body><h1 class='job-title'>工程师</h1>"
                "<div class='company-name'>虚构公司</div>"
                "<div class='job-description'>Python 且 (SQL 或 Airflow)</div>"
                "</body></html>")
        item = ListItem("JT-9", "工程师", "虚构公司", "", "", "http://127.0.0.1/jobs/JT-9")
        parsed = parse_detail_page(html, item.detail_url, "site-a", item,
                                   load_selectors(DEFAULT_SELECTORS))
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.observation.requirement_parse.skills,
                         ("Airflow", "Python", "SQL"))
        self.assertNotIn("requirement_parse", parsed.observation.content_payload())
if __name__ == "__main__": unittest.main(verbosity=2)
