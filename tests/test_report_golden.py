"""Golden report summary/events, deterministic Markdown, and exports."""
from __future__ import annotations
import csv
import json
from pathlib import Path
from jobtrace.clock import FixedClock, parse_instant
from jobtrace.report import build_report, write_reports
from jobtrace.store import Store
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "expected" / "day2_report.json"
def test_day2_report_matches_golden_and_is_deterministic(collect):
    _, _ = collect("baseline", run_id="run-1", fixed_at="2026-01-15T09:00:00Z")
    loaded, outcome = collect("day2", run_id="run-2", fixed_at="2026-01-16T09:00:00Z")
    assert outcome.result.complete is True
    clock = FixedClock(parse_instant("2026-01-16T09:00:00Z"))
    store = Store(loaded.config.storage.database, readonly=True)
    try:
        report = build_report(store, clock, "jobtrace-demo")
        paths = write_reports(store, loaded.config.storage.report_dir, clock, "jobtrace-demo")
        first_markdown = Path(paths["markdown"]).read_bytes()
        write_reports(store, loaded.config.storage.report_dir, clock, "jobtrace-demo")
    finally:
        store.close()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    summary = report["summary"]
    expected = golden["report"]
    assert report["generated_at"] == expected["generated_at"]
    for key in ("jobs_total", "jobs_open", "jobs_closed", "event_counts"):
        assert summary[key] == expected[key]
    assert summary["run_id"] == expected["latest_run_id"]
    assert summary["run_status"] == expected["latest_status"]
    assert (
        report["raw_evidence"]["documents_in_latest_run"]
        == expected["raw_documents_in_latest_run"]
    )
    simplified = [
        {
            "event_type": event["event_type"],
            "external_id": event["external_id"],
            "field": event["field"],
            "old_value": event["old_value"],
            "new_value": event["new_value"],
        }
        for event in report["events"]
    ]
    assert simplified == golden["events"]
    markdown_path = Path(paths["markdown"])
    json_path = Path(paths["json"])
    csv_path = Path(paths["csv"])
    jsonl_path = Path(paths["jsonl"])
    assert b"## \xe7\x94\x9f\xe5\x91\xbd\xe5\x91\xa8\xe6\x9c\x9f\xe4\xba\x8b\xe4\xbb\xb6\xe8\xae\xa1\xe6\x95\xb0" in first_markdown
    assert b"http://" not in first_markdown
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"] == summary
    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == expected["csv_rows"]
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == expected["jsonl_rows"]
    assert json.loads(lines[0])["event_type"] == "CHANGED"
    assert markdown_path.read_bytes() == first_markdown
