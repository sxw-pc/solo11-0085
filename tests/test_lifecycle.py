"""New/change/disappear/close/reopen lifecycle semantics."""
from __future__ import annotations
from jobtrace.store import Store
def _job(store: Store, external_id: str) -> dict:
    job = store.find_job("jobtrace-demo", external_id)
    assert job is not None
    return job
def test_full_lifecycle_and_reopen(collect):
    loaded, baseline = collect("baseline", run_id="run-baseline")
    assert baseline.lifecycle.new_jobs == 64
    _, day2 = collect("day2", run_id="run-day2")
    assert day2.lifecycle.new_jobs == 2
    assert day2.lifecycle.changed_events == 3
    assert day2.lifecycle.changed_fields == {"location": 1, "salary": 1, "title": 1}
    assert day2.lifecycle.disappeared == 2
    assert day2.lifecycle.closed == 0
    _, day2_again = collect("day2", run_id="run-day2-again")
    assert day2_again.lifecycle.closed == 2
    assert day2_again.lifecycle.disappeared == 2
    _, reopened_run = collect("baseline", run_id="run-baseline-again")
    assert reopened_run.lifecycle.reopened == 2
    assert reopened_run.lifecycle.changed_events == 3
    assert reopened_run.lifecycle.disappeared == 2
    store = Store(loaded.config.storage.database)
    try:
        ledger = store.ledger_counts("jobtrace-demo")
        assert ledger == {"total": 66, "open": 66, "closed": 0}
        job = _job(store, "JT-0003")
        assert job["status"] == "open"
        assert job["reopen_count"] == 1
        assert job["closed_at"] is None
        assert _job(store, "JT-0065")["missing_streak"] == 1
        closed_events = store.list_events(run_id="run-day2-again")
        assert {event["event_type"] for event in closed_events} == {"DISAPPEARED", "CLOSED"}
        closed_job_ids = {
            event["external_id"]
            for event in closed_events
            if event["event_type"] == "CLOSED"
        }
        assert closed_job_ids == {"JT-0003", "JT-0017"}
        salary_event = next(
            event
            for event in store.list_events(run_id="run-day2")
            if event["event_type"] == "CHANGED" and event["field"] == "salary"
        )
        assert salary_event["old_value"] != salary_event["new_value"]
    finally:
        store.close()
def test_incomplete_run_never_closes_or_marks_missing(collect):
    loaded, baseline = collect("baseline", run_id="run-baseline")
    assert baseline.result.complete is True
    _, malformed = collect("malformed", run_id="run-malformed")
    assert malformed.result.complete is False
    assert malformed.result.details_failed == 3
    store = Store(loaded.config.storage.database)
    try:
        assert store.event_counts("run-malformed")["DISAPPEARED"] == 0
        for external_id in ("JT-0012", "JT-0022", "JT-0031"):
            job = _job(store, external_id)
            assert job["status"] == "open"
            assert job["missing_streak"] == 0
        assert store.ledger_counts("jobtrace-demo")["closed"] == 0
    finally:
        store.close()
