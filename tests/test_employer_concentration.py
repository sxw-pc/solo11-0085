"""Employer concentration audits: contracts, mutex bounds, versioning, read-only."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobtrace.concentration import RULES_VERSION, analyze_run  # noqa: E402
from jobtrace.employers import AliasRule, Employer, EmployerRegistry, load_registry  # noqa: E402
from jobtrace.errors import ConfigError  # noqa: E402
from jobtrace.report import render_concentration_markdown, write_concentration_files  # noqa: E402
from jobtrace.store import Store  # noqa: E402

DAY = "2026-01-15T09:00:00Z"


def emp(employer_id, group=None):
    return Employer(employer_id, f"{employer_id}法定名（虚构）", group)


def alias(display, employer_id, start="2026-01-01", end="2026-12-31"):
    return AliasRule(display, employer_id, date.fromisoformat(start), date.fromisoformat(end))


def registry(*, employers, aliases, groups=(), mutex=(), version="reg-test"):
    return EmployerRegistry(version, {e.employer_id: e for e in employers},
                            dict(groups), tuple(aliases),
                            tuple(tuple(sorted(pair)) for pair in mutex))


def make_store(runs):
    """runs: (run_id, status, is_complete, observed_at, [(job_uid, company, ext_id?)], unique?)"""
    store = Store(":memory:")
    for row in runs:
        run_id, status, complete, observed_at, jobs = row[:5]
        unique_jobs = row[5] if len(row) > 5 else len(jobs)
        store.begin_run(run_id=run_id, site_key="jobtrace-demo", scenario="baseline",
                        base_url="http://127.0.0.1", config_hash="cfg", started_at=observed_at)
        store.finish_run(run_id, status=status, is_complete=complete,
                         finished_at=observed_at, unique_jobs=unique_jobs,
                         details_ok=len(jobs))
        with store.transaction() as conn:
            for entry in jobs:
                job_uid, company = entry[0], entry[1]
                external_id = entry[2] if len(entry) > 2 else job_uid
                payload = {"external_id": external_id, "title": "合成岗位", "company": company}
                conn.execute(
                    "INSERT INTO job_snapshots(job_uid, run_id, content_hash, payload_json,"
                    " observed_at) VALUES (?, ?, ?, ?, ?)",
                    (job_uid, run_id, f"hash-{job_uid}",
                     json.dumps(payload, ensure_ascii=False), observed_at))
    return store


def shares_of(contract, end="min"):
    return {row["id"]: row for row in contract["witnesses"][end]["shares"]}


class ConcentrationTest(unittest.TestCase):
    def test_legal_and_group_contracts_from_spec_example(self):
        reg = registry(
            employers=[emp("EMP-A", "GRP-G"), emp("EMP-B", "GRP-G"), emp("EMP-C")],
            groups=[("GRP-G", "集团G（虚构）")],
            aliases=[alias("甲公司", "EMP-A"), alias("乙公司", "EMP-B"), alias("丙公司", "EMP-C")])
        jobs = ([(f"job-a{i}", "甲公司") for i in range(5)]
                + [(f"job-b{i}", "乙公司") for i in range(3)]
                + [(f"job-c{i}", "丙公司") for i in range(2)])
        store = make_store([("run-1", "complete", True, DAY, jobs)])
        self.addCleanup(store.close)
        result = analyze_run(store, "run-1", reg)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["postings"],
                         {"total": 10, "known": 10, "unknown": 0, "unobserved": 0})
        legal, group = result["contracts"]["legal"], result["contracts"]["group"]
        self.assertEqual((legal["denominator"], group["denominator"]), (10, 10))
        self.assertEqual(legal["hhi"], {"min": 0.38, "max": 0.38, "point": True})
        self.assertEqual(group["hhi"], {"min": 0.68, "max": 0.68, "point": True})
        group_shares = shares_of(group)
        self.assertEqual(group_shares["GRP-G"]["postings"], 8)
        self.assertEqual(group_shares["GRP-G"]["name"], "集团G（虚构）")
        self.assertEqual(group_shares["EMP-C"]["postings"], 2)
        for contract in (legal, group):
            for end in ("min", "max"):
                self.assertEqual(len(contract["witnesses"][end]["jobs"]), 10)
        self.assertEqual(legal["witnesses"]["min"]["jobs"]["job-a0"], "EMP-A")
        self.assertEqual(group["witnesses"]["min"]["jobs"]["job-b0"], "GRP-G")

    def test_cross_site_posts_count_independently_without_merging(self):
        # 同一雇主在两个站点的相同内容岗位是两次独立刊登，不推断为同一岗位实体。
        reg = registry(employers=[emp("EMP-A")], aliases=[alias("甲公司", "EMP-A")])
        jobs = [("job_alpha", "甲公司", "JT-0001"), ("job_beta", "甲公司", "JT-0001"),
                ("job_gamma", "甲公司", "JT-0002")]
        store = make_store([("run-1", "complete", True, DAY, jobs)])
        self.addCleanup(store.close)
        result = analyze_run(store, "run-1", reg)
        legal = result["contracts"]["legal"]
        self.assertEqual(result["postings"]["total"], 3)
        self.assertEqual(legal["hhi"], {"min": 1.0, "max": 1.0, "point": True})
        witness = legal["witnesses"]["min"]
        self.assertEqual(sorted(witness["jobs"]), ["job_alpha", "job_beta", "job_gamma"])
        self.assertEqual(set(witness["jobs"].values()), {"EMP-A"})

    def test_same_name_reuse_by_date_and_rename_stays_one_entity(self):
        reg = registry(
            employers=[emp("EMP-A"), emp("EMP-D")],
            aliases=[
                alias("品牌X", "EMP-A", "2025-01-01", "2025-06-30"),
                alias("品牌X", "EMP-D", "2025-07-01", "2026-12-31"),
                alias("A公司", "EMP-A", "2025-01-01", "2025-12-31"),
                alias("A科技", "EMP-A", "2026-01-01", "2026-12-31"),
            ])
        store = make_store([
            ("run-2025", "complete", True, "2025-03-01T09:00:00Z",
             [("j1", "品牌X"), ("j2", "A公司")]),
            ("run-2026", "complete", True, DAY, [("j3", "品牌X"), ("j4", "A科技")]),
        ])
        self.addCleanup(store.close)
        old = analyze_run(store, "run-2025", reg)
        self.assertEqual(set(shares_of(old["contracts"]["legal"])), {"EMP-A"})
        new = analyze_run(store, "run-2026", reg)
        new_shares = shares_of(new["contracts"]["legal"])
        self.assertEqual({key: row["postings"] for key, row in new_shares.items()},
                         {"EMP-A": 1, "EMP-D": 1})
        self.assertEqual(new["contracts"]["legal"]["hhi"]["max"], 0.5)

    def test_shared_candidates_mutex_bounds_are_global_not_stitched(self):
        reg = registry(
            employers=[emp("EMP-A"), emp("EMP-B"), emp("EMP-C")],
            aliases=[
                alias("甲牌", "EMP-A"), alias("甲牌", "EMP-B"),
                alias("乙牌", "EMP-A"), alias("乙牌", "EMP-B"),
                alias("A公司", "EMP-A"), alias("丙公司", "EMP-C"),
            ],
            mutex=[("甲牌", "乙牌")])
        jobs = ([("j-m1", "甲牌"), ("j-m2", "甲牌"), ("j-y1", "乙牌")]
                + [(f"j-a{i}", "A公司") for i in range(4)]
                + [(f"j-c{i}", "丙公司") for i in range(3)])
        store = make_store([("run-1", "complete", True, DAY, jobs)])
        self.addCleanup(store.close)
        result = analyze_run(store, "run-1", reg)
        legal = result["contracts"]["legal"]
        self.assertEqual(result["status"], "ok")
        # 逐岗位拼接互斥选择会得到 0.34；全局指派的可行区间是 [0.38, 0.46]。
        self.assertEqual((legal["hhi"]["min"], legal["hhi"]["max"]), (0.38, 0.46))
        self.assertFalse(legal["hhi"]["point"])
        self.assertTrue(any(r["code"] == "ambiguous_alias" for r in result["reasons"]))
        low, high = legal["witnesses"]["min"], legal["witnesses"]["max"]
        self.assertEqual(len(low["jobs"]), 10)
        self.assertEqual(low["jobs"]["j-m1"], low["jobs"]["j-m2"])
        self.assertNotEqual(low["jobs"]["j-m1"], low["jobs"]["j-y1"])
        self.assertEqual(low["jobs"]["j-m1"], "EMP-B")
        self.assertEqual(high["jobs"]["j-m1"], "EMP-A")

    def test_old_run_rereads_original_snapshot_and_registry_version(self):
        v1 = registry(version="reg-1", employers=[emp("EMP-A")],
                      aliases=[alias("品牌X", "EMP-A", "2025-01-01", "2025-12-31")])
        v2 = registry(version="reg-2", employers=[emp("EMP-D")],
                      aliases=[alias("品牌X", "EMP-D", "2025-01-01", "2025-12-31")])
        store = make_store([("run-old", "complete", True, "2025-03-01T09:00:00Z",
                             [("j1", "品牌X"), ("j2", "品牌X")])])
        self.addCleanup(store.close)
        with store.transaction() as conn:  # 后续 run 改名只影响 jobs 当前行
            conn.execute(
                "INSERT INTO jobs(job_uid, site_key, external_id, title, company, detail_url,"
                " first_seen_run_id, first_seen_at, last_seen_run_id, last_seen_at,"
                " content_hash, updated_at) VALUES ('j1','jobtrace-demo','X1','合成岗位',"
                " 'X集团','http://127.0.0.1/jobs/X1','run-old','2025-03-01T09:00:00Z',"
                " 'run-new','2026-01-15T09:00:00Z','h','2026-01-15T09:00:00Z')")
        first = analyze_run(store, "run-old", v1)
        self.assertEqual(first["registry_version"], "reg-1")
        self.assertEqual(first["rules_version"], RULES_VERSION)
        self.assertEqual(first["contracts"]["legal"]["witnesses"]["min"]["jobs"]["j1"], "EMP-A")
        second = analyze_run(store, "run-old", v2)
        self.assertEqual(second["contracts"]["legal"]["witnesses"]["min"]["jobs"]["j1"], "EMP-D")
        third = analyze_run(store, "run-old", v1)
        self.assertEqual(third["contracts"]["legal"]["witnesses"]["min"]["jobs"]["j1"], "EMP-A")
        self.assertEqual(first["input_fingerprint"], third["input_fingerprint"])

    def test_incomplete_or_missing_run_is_not_a_market_snapshot(self):
        reg = registry(employers=[emp("EMP-A")], aliases=[alias("甲公司", "EMP-A")])
        store = make_store([("run-inc", "incomplete", False, DAY, [("j1", "甲公司")])])
        self.addCleanup(store.close)
        result = analyze_run(store, "run-inc", reg)
        self.assertEqual(result["status"], "incomplete_run")
        self.assertEqual(result["reasons"][0]["code"], "incomplete_run")
        for contract in result["contracts"].values():
            self.assertIsNone(contract["hhi"])
        self.assertEqual(analyze_run(store, "run-nope", reg)["status"], "run_not_found")

    def test_unknown_sources_block_exact_hhi_without_singletons(self):
        reg = registry(employers=[emp("EMP-A"), emp("EMP-C")],
                       aliases=[alias("甲公司", "EMP-A"), alias("丙公司", "EMP-C")])
        jobs = ([(f"j-a{i}", "甲公司") for i in range(5)]
                + [(f"j-c{i}", "丙公司") for i in range(3)]
                + [("j-x1", "未知企业"), ("j-x2", "未知企业"), ("j-x3", "")])
        store = make_store([("run-1", "complete", True, DAY, jobs, 13)])
        self.addCleanup(store.close)
        result = analyze_run(store, "run-1", reg)
        self.assertEqual(result["status"], "unresolvable")
        self.assertEqual(result["postings"],
                         {"total": 11, "known": 8, "unknown": 3, "unobserved": 2})
        codes = {r["code"] for r in result["reasons"]}
        self.assertEqual(codes, {"unknown_company", "missing_company", "coverage_gap"})
        unknown = {e["display"]: e for e in result["unknown_companies"]}
        self.assertEqual(unknown["未知企业"]["postings"], 2)
        self.assertEqual(unknown[""]["reason"], "missing_company")
        for contract in result["contracts"].values():
            self.assertIsNone(contract["hhi"])
            self.assertIsNone(contract["witnesses"])
        markdown = render_concentration_markdown(result)
        self.assertIn("未知企业", markdown)
        self.assertIn("无法得出", markdown)

    def test_analysis_is_read_only_and_forces_no_changed_events(self):
        reg = registry(employers=[emp("EMP-A")], aliases=[alias("甲公司", "EMP-A")])
        store = make_store([("run-1", "complete", True, DAY,
                             [("j1", "甲公司"), ("j2", "甲公司")])])
        self.addCleanup(store.close)
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO events(job_uid, run_id, event_type, field, old_value, new_value,"
                " created_at) VALUES ('j1','run-1','NEW','job',NULL,'合成岗位',?)", (DAY,))
        before = (store.scalar("SELECT COUNT(*) FROM events"),
                  store.scalar("SELECT COUNT(*) FROM jobs"), store.run_snapshots("run-1"))
        analyze_run(store, "run-1", reg)
        upgraded = registry(version="reg-upgraded", employers=[emp("EMP-A")],
                            aliases=[alias("甲公司", "EMP-A")])
        analyze_run(store, "run-1", upgraded)  # 规则版次升级也不得伪造 CHANGED 事件
        self.assertEqual(store.scalar("SELECT COUNT(*) FROM events"), before[0])
        self.assertEqual(store.scalar("SELECT COUNT(*) FROM events"
                                      " WHERE event_type = 'CHANGED'"), 0)
        self.assertEqual(store.scalar("SELECT COUNT(*) FROM jobs"), before[1])
        self.assertEqual(store.run_snapshots("run-1"), before[2])

    def test_infeasible_mutex_and_empty_run(self):
        reg = registry(employers=[emp("EMP-A")],
                       aliases=[alias("甲牌", "EMP-A"), alias("乙牌", "EMP-A")],
                       mutex=[("甲牌", "乙牌")])
        store = make_store([("run-1", "complete", True, DAY, [("j1", "甲牌"), ("j2", "乙牌")]),
                            ("run-empty", "complete", True, DAY, [], 0)])
        self.addCleanup(store.close)
        self.assertEqual(analyze_run(store, "run-1", reg)["status"], "infeasible")
        self.assertEqual(analyze_run(store, "run-empty", reg)["status"], "empty")

    def test_registry_yaml_loader_and_repo_file(self):
        repo = load_registry(ROOT / "configs" / "employers.yaml")
        self.assertEqual(repo.version, "2026.1")
        self.assertEqual(repo.candidates("星砂科技（虚构）", date(2026, 1, 15)), ("EMP-XINGSHA",))
        self.assertEqual(repo.candidates("星砂科技", date(2026, 1, 15)), ("EMP-XINGSHA",))
        self.assertEqual(repo.candidates("青禾数据（虚构）", date(2025, 6, 1)), ("EMP-SHILI",))
        self.assertEqual(repo.candidates("青禾数据（虚构）", date(2026, 6, 1)), ("EMP-QINGHE",))
        self.assertEqual(repo.candidates("未注册", date(2026, 1, 15)), ())
        self.assertEqual(repo.group_of("EMP-XINGSHA"), "GRP-XINGCHEN")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yaml"
            bad.write_text(
                'registry_version: "bad"\n'
                "employers:\n  - id: EMP-A\n    legal_name: 甲（虚构）\n"
                "aliases:\n  - display: 甲\n    employer: EMP-NOPE\n"
                '    valid_from: "2026-01-01"\n    valid_to: "2026-12-31"\n',
                encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_registry(bad)

    def test_report_files_carry_audit_trail(self):
        reg = registry(employers=[emp("EMP-A", "GRP-G"), emp("EMP-C")],
                       groups=[("GRP-G", "集团G（虚构）")],
                       aliases=[alias("甲公司", "EMP-A"), alias("丙公司", "EMP-C")])
        store = make_store([("run-1", "complete", True, DAY,
                             [("j1", "甲公司"), ("j2", "丙公司")])])
        self.addCleanup(store.close)
        result = analyze_run(store, "run-1", reg)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_concentration_files(result, tmp)
            payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertIn("法律雇主合同", markdown)
        self.assertIn("集团管控合同", markdown)
        self.assertIn("j1", markdown)


if __name__ == "__main__":
    unittest.main()
