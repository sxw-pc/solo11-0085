"""Offline historical corrections: archive vs recompute semantics."""

from __future__ import annotations

import json

import pytest

from jobtrace.backfill import ARCHIVE, RECOMPUTE, submit_correction
from jobtrace.clock import FixedClock, parse_instant
from jobtrace.errors import CorrectionError
from jobtrace.lifecycle import LifecycleEngine
from jobtrace.models import JobObservation
from jobtrace.normalize import Experience
from jobtrace.report import build_report
from jobtrace.salary import parse_salary
from jobtrace.store import Store

SITE = "jobtrace-demo"
DAY1, DAY2, DAY3 = "2026-03-01", "2026-03-02", "2026-03-03"
NOW = "2026-03-05T10:00:00Z"


def _obs(external_id: str, *, title: str = "合成工程师", salary: str = "10k-20k"):
    return JobObservation(
        site_key=SITE, external_id=external_id, title=title, company="合成公司",
        location_raw="上海·浦东", city="上海", district="浦东",
        salary=parse_salary(salary),
        experience=Experience(text="不限", min_years=None, max_years=None),
        employment_type="全职", description="", tags=(),
        detail_url=f"http://127.0.0.1/jobs/{external_id}",
    )


def _run_day(store, run_id, business_date, observations, *, complete=True):
    store.begin_run(run_id=run_id, site_key=SITE, scenario="synthetic",
                    base_url="http://127.0.0.1", config_hash="synthetic",
                    started_at=f"{business_date}T09:00:00Z", business_date=business_date)
    LifecycleEngine(store).apply(
        run_id=run_id, site_key=SITE, observations=observations, complete=complete,
        now=f"{business_date}T09:05:00Z", close_after_missing_runs=2)
    store.finish_run(run_id, status="complete" if complete else "incomplete",
                     is_complete=complete, finished_at=f"{business_date}T09:06:00Z")


def _correct(store, *, correction_id="corr-day2", corrected_run_id="run-d2",
             business_date=DAY2, mode=RECOMPUTE, observations=None, hook=None):
    return submit_correction(
        store, correction_id=correction_id, corrected_run_id=corrected_run_id,
        business_date=business_date, source_summary="迟到的第 2 天本地 mock 补采批次",
        mode=mode, observations=observations or [_obs("JT-A"), _obs("JT-B")],
        now=NOW, close_after_missing_runs=2, hook=hook)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "backfill.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def three_day(store):
    """Days 2 and 3 miss JT-A, so the job is closed before any correction."""
    _run_day(store, "run-d1", DAY1, [_obs("JT-A"), _obs("JT-B")])
    _run_day(store, "run-d2", DAY2, [_obs("JT-B")])
    _run_day(store, "run-d3", DAY3, [_obs("JT-B")])
    return store


def test_three_day_archive_keeps_current_state(three_day):
    store = three_day
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "closed" and job["missing_streak"] == 2
    events_before = store.list_events(limit=10000)
    result = _correct(store, mode=ARCHIVE)
    assert result.status == "applied" and result.mode == ARCHIVE
    assert "证据归档" in result.semantics
    assert result.diff == {"appeared": ["JT-A"], "vanished": [], "changed": []}
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "closed" and job["missing_streak"] == 2
    assert store.list_events(limit=10000) == events_before
    correction = store.get_correction("corr-day2")
    assert correction["mode"] == ARCHIVE and correction["status"] == "applied"
    run = store.get_run("corr-day2")
    assert run["is_complete"] == 1 and run["business_date"] == DAY2
    assert store.scalar(
        "SELECT COUNT(*) FROM job_snapshots WHERE run_id = 'corr-day2'") == 2
    report = build_report(store, FixedClock(parse_instant("2026-03-06T00:00:00Z")), SITE)
    assert report["summary"]["corrections"] == 1
    assert report["summary"]["jobs_closed"] == 1
    assert report["corrections"][0]["semantics"] == result.semantics
    assert report["corrections"][0]["diff"]["appeared"] == ["JT-A"]


def test_three_day_recompute_replays_business_time(three_day):
    store = three_day
    result = _correct(store, mode=RECOMPUTE)
    assert result.status == "applied" and "生命周期重算" in result.semantics
    assert result.affected_run_ids == ["corr-day2", "run-d3"]
    assert result.events_archived == 3 and result.events_emitted == 1
    assert result.ledger == {"total": 2, "open": 2, "closed": 0}
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "open" and job["missing_streak"] == 1
    assert job["closed_at"] is None and job["last_seen_run_id"] == "corr-day2"
    assert store.event_counts("run-d1")["NEW"] == 2
    assert store.event_counts("run-d2")["DISAPPEARED"] == 0
    assert not any(store.list_events(run_id="corr-day2"))
    day3 = store.list_events(run_id="run-d3")
    assert [(event["event_type"], event["new_value"]) for event in day3] == [
        ("DISAPPEARED", "missing_1")]
    archived_events = store.scalar(
        "SELECT COUNT(*) FROM derived_archive"
        " WHERE correction_id = 'corr-day2' AND kind = 'event'")
    assert archived_events == 3
    archived_jobs = store.scalar(
        "SELECT COUNT(*) FROM derived_archive"
        " WHERE correction_id = 'corr-day2' AND kind = 'job_state'")
    assert archived_jobs == 2
    report = build_report(store, FixedClock(parse_instant("2026-03-06T00:00:00Z")), SITE)
    assert report["summary"]["jobs_open"] == 2 and report["summary"]["jobs_closed"] == 0
    assert report["corrections"][0]["mode"] == RECOMPUTE


def test_duplicate_correction_is_idempotent(three_day):
    store = three_day
    first = _correct(store)
    assert first.status == "applied"
    events_after_first = store.list_events(limit=10000)
    second = _correct(store)
    assert second.status == "duplicate"
    assert second.semantics == first.semantics
    assert store.list_events(limit=10000) == events_after_first
    assert store.scalar("SELECT COUNT(*) FROM corrections") == 1
    assert store.scalar("SELECT COUNT(*) FROM runs WHERE run_id = 'corr-day2'") == 1
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "open" and job["missing_streak"] == 1
    with pytest.raises(CorrectionError):
        _correct(store, observations=[_obs("JT-A")])
    with pytest.raises(CorrectionError):
        _correct(store, correction_id="corr-day2-again")


def test_incomplete_batch_between_days_is_not_missing_evidence(store):
    _run_day(store, "run-d1", DAY1, [_obs("JT-A"), _obs("JT-B")])
    _run_day(store, "run-mid", DAY2, [_obs("JT-B", title="合成运营")], complete=False)
    _run_day(store, "run-d3", DAY3, [_obs("JT-B")])
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "open" and job["missing_streak"] == 1
    result = _correct(
        store, correction_id="corr-d1", corrected_run_id="run-d1", business_date=DAY1,
        observations=[_obs("JT-A", salary="20k-30k"), _obs("JT-B")])
    assert result.status == "applied"
    assert result.affected_run_ids == ["corr-d1", "run-mid", "run-d3"]
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "open" and job["missing_streak"] == 1
    assert job["salary_text"] == parse_salary("20k-30k").normalized
    mid_events = store.list_events(run_id="run-mid")
    assert {event["event_type"] for event in mid_events} == {"CHANGED"}
    assert store.event_counts("run-mid")["DISAPPEARED"] == 0
    job_b = store.find_job(SITE, "JT-B")
    assert job_b["title"] == "合成工程师"


def test_interrupted_recompute_rolls_back_and_recovers(three_day):
    store = three_day

    def crash(stage):
        if stage == "events":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        _correct(store, hook=crash)
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "closed" and job["missing_streak"] == 2
    assert store.event_counts("run-d3")["CLOSED"] == 1
    assert store.get_correction("corr-day2") is None
    assert store.get_run("corr-day2") is None
    assert store.scalar("SELECT COUNT(*) FROM derived_archive") == 0
    result = _correct(store)
    assert result.status == "applied"
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "open" and job["missing_streak"] == 1
    day3 = store.list_events(run_id="run-d3")
    assert [(event["event_type"], event["new_value"]) for event in day3] == [
        ("DISAPPEARED", "missing_1")]
    assert store.scalar("SELECT COUNT(*) FROM events") == 3
    assert store.scalar(
        "SELECT COUNT(*) FROM derived_archive WHERE kind = 'event'") == 3


def test_unlinked_complete_batches_refuse_auto_adjudication(store):
    _run_day(store, "run-d1", DAY1, [_obs("JT-A")])
    _run_day(store, "run-d1-late", DAY1, [_obs("JT-A", salary="15k-25k")])
    with pytest.raises(CorrectionError):
        _correct(store, correction_id="corr-d1", corrected_run_id="run-d1",
                 business_date=DAY1, observations=[_obs("JT-A", salary="20k-30k")])
    assert store.get_correction("corr-d1") is None
    assert store.scalar("SELECT COUNT(*) FROM events") == 2
    job = store.find_job(SITE, "JT-A")
    assert job["salary_text"] == parse_salary("15k-25k").normalized
    result = _correct(store, correction_id="corr-d1-archive", corrected_run_id="run-d1",
                    business_date=DAY1, mode=ARCHIVE,
                    observations=[_obs("JT-A", salary="20k-30k")])
    assert result.status == "applied"
    assert result.diff["changed"] == ["JT-A"]


def test_recompute_removes_job_introduced_by_superseded_batch(store):
    _run_day(store, "run-d1", DAY1, [_obs("JT-A")])
    _run_day(store, "run-d2", DAY2, [_obs("JT-A"), _obs("JT-C")])
    _run_day(store, "run-d3", DAY3, [_obs("JT-A")])
    result = _correct(store, observations=[_obs("JT-A")])
    assert result.status == "applied"
    assert result.affected_run_ids == ["corr-day2", "run-d3"]
    assert store.find_job(SITE, "JT-C") is None
    assert store.find_job(SITE, "JT-A")["status"] == "open"
    assert store.scalar("SELECT COUNT(*) FROM events") == 1
    assert store.scalar(
        "SELECT COUNT(*) FROM derived_archive WHERE kind = 'event'") == 2
    assert store.scalar(
        "SELECT COUNT(*) FROM derived_archive WHERE kind = 'job_state'") == 2


def test_chained_recompute_corrects_latest_batch(three_day):
    store = three_day
    first = _correct(store)
    assert first.status == "applied"
    assert store.find_job(SITE, "JT-A")["status"] == "open"
    result = _correct(store, correction_id="corr-day2-v2",
                      corrected_run_id="corr-day2", observations=[_obs("JT-B")])
    assert result.status == "applied"
    assert result.diff["vanished"] == ["JT-A"]
    job = store.find_job(SITE, "JT-A")
    assert job["status"] == "closed" and job["missing_streak"] == 2
    day3 = store.list_events(run_id="run-d3")
    assert [(event["event_type"], event["new_value"]) for event in day3] == [
        ("DISAPPEARED", "missing_2"), ("CLOSED", "closed")]
    assert store.scalar("SELECT COUNT(*) FROM corrections") == 2


def test_pipeline_business_date_and_correction_from_evidence(collect):
    collect("baseline", run_id="run-1", fixed_at="2026-01-15T09:00:00Z")
    collect("day2", run_id="run-2", fixed_at="2026-01-16T09:00:00Z")
    loaded, _ = collect("day2", run_id="run-3", fixed_at="2026-01-17T09:00:00Z")
    store = Store(loaded.config.storage.database)
    try:
        assert store.get_run("run-2")["business_date"] == "2026-01-16"
        job = store.find_job(SITE, "JT-0003")
        assert job["status"] == "closed" and job["missing_streak"] == 2
        payloads = [
            json.loads(row["payload_json"]) for row in store.run_observations("run-1")
        ]
        result = submit_correction(
            store, correction_id="corr-run-2", corrected_run_id="run-2",
            business_date="2026-01-16", source_summary="迟到的第 2 天完整采集",
            mode=RECOMPUTE, observations=payloads, now="2026-01-18T09:00:00Z",
            close_after_missing_runs=2)
        assert result.status == "applied"
        assert result.affected_run_ids == ["corr-run-2", "run-3"]
        job = store.find_job(SITE, "JT-0003")
        assert job["status"] == "open" and job["missing_streak"] == 1
        assert store.find_job(SITE, "JT-0065")["first_seen_run_id"] == "run-3"
        day3_counts = store.event_counts("run-3")
        assert day3_counts["DISAPPEARED"] == 2 and day3_counts["CLOSED"] == 0
        assert store.event_counts("run-2")["NEW"] == 0
        report = build_report(
            store, FixedClock(parse_instant("2026-01-18T10:00:00Z")), SITE)
        assert report["corrections"][0]["corrected_run_id"] == "run-2"
        assert report["summary"]["jobs_closed"] == 0
    finally:
        store.close()
