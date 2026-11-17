"""Pagination loop/limit guards, cursor persistence, and resume behavior."""
from __future__ import annotations
import pytest
from jobtrace.errors import PaginationError, PaginationLimitError, PaginationLoopError
from jobtrace.pagination import Cursor, PaginationGuard, normalized_page_url
from jobtrace.pipeline import run_collection
def test_natural_end_and_detailed_url_normalization():
    guard = PaginationGuard(max_pages=5)
    guard.start("http://127.0.0.1:8765/jobs?b=2&a=1")
    assert guard.advance(None) is None
    assert guard.reason == "natural_end"
    assert normalized_page_url("HTTP://127.0.0.1:8765/jobs?b=2&a=1#x") == (
        "http://127.0.0.1:8765/jobs?a=1&b=2"
    )
def test_repeated_url_raises_loop_error():
    guard = PaginationGuard(max_pages=10)
    guard.start("http://127.0.0.1:8765/jobs?page=1")
    guard.advance("http://127.0.0.1:8765/jobs?page=2")
    with pytest.raises(PaginationLoopError): guard.advance("http://127.0.0.1:8765/jobs?page=1")
    assert guard.reason == "loop"
def test_max_page_limit_raises_before_exceeding():
    guard = PaginationGuard(max_pages=2)
    guard.start("http://127.0.0.1:8765/jobs?page=1")
    assert guard.advance("http://127.0.0.1:8765/jobs?page=2")
    with pytest.raises(PaginationLimitError):
        guard.advance("http://127.0.0.1:8765/jobs?page=3")
    assert guard.page_count == 2
def test_cursor_round_trip_and_invalid_payload(tmp_path):
    path = tmp_path / "state" / "checkpoint.json"
    cursor = Cursor(version=1, site_key="jobtrace-demo",
                    next_url="http://127.0.0.1:8765/jobs?page=3",
                    visited_pages=["http://127.0.0.1:8765/jobs?page=1"],
                    page_count=1, completed=False, updated_at="2026-01-15T09:00:00Z")
    cursor.save(path)
    loaded = Cursor.load(path)
    assert loaded == cursor
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(PaginationError): Cursor.load(path)
def test_full_crawl_stops_on_pagination_loop(collect):
    _, outcome = collect("pagination_loop", run_id="run-loop")
    assert outcome.result.complete is False
    assert outcome.result.stop_reason == "pagination_loop"
    assert outcome.result.pages_fetched == 7
    assert outcome.result.unique_jobs == 64
    assert any("repeated pagination URL" in warning for warning in outcome.result.warnings)
def test_resume_starts_from_saved_cursor_and_marks_incomplete(live_mock, loaded_factory, tmp_path):
    with live_mock("baseline") as (base_url, _server):
        loaded = loaded_factory(base_url)
        Cursor(version=1, site_key=loaded.config.site.name,
               next_url=f"{base_url}/jobs?page=2",
               visited_pages=[f"{base_url}/jobs?page=1"], page_count=1,
               completed=False, updated_at="2026-01-15T09:00:00Z").save(
                   loaded.config.storage.checkpoint)
        outcome = run_collection(
            loaded,
            scenario="baseline",
            run_id="run-resume",
            resume=True,
            sleep=lambda _seconds: None,
        )
    assert outcome.result.resumed is True
    assert outcome.result.pages_fetched == 6
    assert outcome.result.unique_jobs == 54
    assert outcome.result.complete is False
    assert outcome.result.stop_reason == "resumed_partial"
    checkpoint = Cursor.load(loaded.config.storage.checkpoint)
    assert checkpoint is not None and checkpoint.completed is True
