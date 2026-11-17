"""The repository must stay inside its declared source line budget."""

from __future__ import annotations

from pathlib import Path

from jobtrace.lint_size import check

ROOT = Path(__file__).resolve().parents[1]


def test_line_budget_and_per_file_limit():
    report = check(ROOT)
    assert 2500 <= report.total <= 6000
    assert report.oversized == {}
    assert report.files["README.md"] <= 300
    assert report.files["jobtrace/lifecycle.py"] <= 300
    assert report.files["tests/test_e2e_demo.py"] <= 300
    assert report.docs_files
