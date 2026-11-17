"""Idempotent upserts, stable identities, and duplicate listing protection."""

from __future__ import annotations

from jobtrace.store import Store


def test_repeated_baseline_is_idempotent(collect):
    loaded, first = collect("baseline", run_id="run-1")
    _, second = collect("baseline", run_id="run-2")
    assert first.result.unique_jobs == 64
    assert second.result.unique_jobs == 64
    assert second.lifecycle.new_jobs == 0
    assert second.lifecycle.changed_events == 0
    store = Store(loaded.config.storage.database)
    try:
        assert len(store.list_jobs()) == 64
        assert len(store.list_runs()) == 2
        assert store.event_counts("run-1")["NEW"] == 64
        assert store.event_counts("run-2")["NEW"] == 0
        assert store.scalar("SELECT COUNT(*) FROM job_snapshots") == 128
        assert len(store.raw_documents_for_run("run-1")) == 71
        assert len(store.raw_documents_for_run("run-2")) == 71
        assert store.scalar("SELECT COUNT(*) FROM raw_documents") == 71
        assert store.scalar("SELECT COUNT(DISTINCT job_uid) FROM jobs") == 64
    finally:
        store.close()


def test_duplicate_listing_id_creates_one_job(collect):
    loaded, outcome = collect("day2", run_id="run-day2")
    assert outcome.result.list_cards == 65
    assert outcome.result.unique_jobs == 64
    assert outcome.result.duplicate_ids == 1
    store = Store(loaded.config.storage.database)
    try:
        jobs = store.list_jobs()
        assert len(jobs) == 64
        assert len({job["external_id"] for job in jobs}) == 64
        assert store.find_job("jobtrace-demo", "JT-0065") is not None
        assert store.scalar(
            "SELECT COUNT(*) FROM jobs WHERE external_id = 'JT-0065'"
        ) == 1
    finally:
        store.close()
