"""End-to-end one-shot demo through the real CLI and loopback servers."""
from __future__ import annotations
from pathlib import Path
from jobtrace.cli import main
from jobtrace.store import Store
def test_demo_no_serve_runs_collection_and_writes_reports(tmp_path, capsys):
    database = tmp_path / "demo.sqlite3"
    report_dir = tmp_path / "reports"
    exit_code = main(
        ["demo", "--no-serve", "--scenario", "baseline", "--port", "0", "--review-port", "0",
         "--db", str(database), "--raw-dir", str(tmp_path / "raw"),
         "--report-dir", str(report_dir), "--checkpoint", str(tmp_path / "checkpoint.json"),
         "--clock", "2026-01-15T09:00:00Z", "--run-id", "run-demo"]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Mock site: http://127.0.0.1:" in output
    assert "Review console: http://127.0.0.1:" in output
    assert "complete=true" in output
    assert f"Report: {report_dir / 'report.md'}" in output
    assert (report_dir / "report.md").is_file()
    assert (report_dir / "report.json").is_file()
    assert (report_dir / "jobs.csv").is_file()
    assert (report_dir / "events.jsonl").is_file()
    assert len(Path(report_dir / "jobs.csv").read_text(encoding="utf-8").splitlines()) == 65
    store = Store(database, readonly=True)
    try:
        assert store.ledger_counts("jobtrace-demo") == {"total": 64, "open": 64, "closed": 0}
        assert len(store.raw_documents_for_run("run-demo")) == 71
    finally:
        store.close()
