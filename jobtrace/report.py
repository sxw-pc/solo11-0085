"""Deterministic Markdown/JSON reports and CSV/JSONL exports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .clock import Clock, iso_z, parse_instant
from .exporters import (
    CSV_COLUMNS,
    EVENT_TYPES,
    EXPORT_COLUMNS,
    render_bundle,
    render_csv,
    render_json,
    render_jsonl,
    render_markdown,
)
from .fingerprint import canonical_json, sha256_text
from .store import Store

__all__ = [
    "CSV_COLUMNS", "EVENT_TYPES", "EXPORT_COLUMNS", "build_report",
    "compute_identity", "identity_payload", "render_bundle", "render_csv",
    "render_json", "render_jsonl", "render_markdown", "write_reports",
]


def _timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return iso_z(parse_instant(value))
    except Exception:
        return value


def _tags(row: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(row.get("tags_json") or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_uid": row["job_uid"], "external_id": row["external_id"],
        "title": row["title"], "company": row["company"],
        "location_raw": row["location_raw"], "city": row["city"],
        "district": row["district"], "salary_text": row["salary_text"],
        "salary_min": row["salary_min"], "salary_max": row["salary_max"],
        "salary_months": row["salary_months"],
        "salary_negotiable": bool(row["salary_negotiable"]),
        "experience_text": row["experience_text"],
        "employment_type": row["employment_type"], "tags": _tags(row),
        "status": row["status"], "missing_streak": row["missing_streak"],
        "reopen_count": row["reopen_count"],
        "first_seen_run_id": row["first_seen_run_id"],
        "first_seen_at": _timestamp(row["first_seen_at"]),
        "last_seen_run_id": row["last_seen_run_id"],
        "last_seen_at": _timestamp(row["last_seen_at"]),
        "closed_at": _timestamp(row["closed_at"]),
        "content_hash": row["content_hash"], "detail_url": row["detail_url"],
    }


def _run_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"], "scenario": run["scenario"],
        "started_at": _timestamp(run["started_at"]),
        "finished_at": _timestamp(run["finished_at"]),
        "status": run["status"], "is_complete": bool(run["is_complete"]),
        "pages_fetched": run["pages_fetched"], "list_cards": run["list_cards"],
        "unique_jobs": run["unique_jobs"], "details_ok": run["details_ok"],
        "details_failed": run["details_failed"],
        "duplicate_ids": run["duplicate_ids"],
        "parse_warnings": run["parse_warnings"], "resumed": bool(run["resumed"]),
        "stop_reason": run["stop_reason"], "config_hash": run["config_hash"],
    }


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": row["event_type"], "field": row["field"],
        "old_value": row["old_value"], "new_value": row["new_value"],
        "created_at": _timestamp(row["created_at"]), "job_uid": row["job_uid"],
        "external_id": row["external_id"], "title": row["title"],
        "company": row["company"],
    }


def _run_evidence(store: Store, run: dict[str, Any]) -> tuple[dict[str, Any], str]:
    evidence = dict(run)
    evidence["rowid"] = store.scalar(
        "SELECT rowid FROM runs WHERE run_id = ?", (run["run_id"],)
    )
    return evidence, sha256_text(canonical_json(evidence))


def identity_payload(report: dict[str, Any]) -> dict[str, Any]:
    """The canonical observation version shared by all four formats."""

    summary, bundle = report["summary"], report.get("bundle", {})
    return {
        "schema_version": 1, "site_key": report["site_key"],
        "run_id": summary["run_id"], "run_started_at": summary["run_started_at"],
        "run_finished_at": summary["run_finished_at"],
        "run_complete": summary["run_complete"],
        "run_evidence_digest": bundle.get("run_evidence_digest"),
        "jobs_total": summary["jobs_total"], "jobs_open": summary["jobs_open"],
        "jobs_closed": summary["jobs_closed"],
        "event_counts": summary["event_counts"],
        "jobs": report["jobs"], "events": report["events"],
    }


def compute_identity(report: dict[str, Any]) -> tuple[str, str]:
    digest = sha256_text(canonical_json(identity_payload(report)))
    return "bundle-" + digest[:24], digest


def build_report(store: Store, clock: Clock, site_key: str | None = None,
                 *, run_id: str | None = None) -> dict[str, Any]:
    """Build one report from a single pinned SQLite read view.

    Every query, including the run selection, ledger counts, jobs and events,
    observes the same commit point, so a concurrent R2 can never leak R2 jobs
    into a report labelled R1.
    """

    with store.read_snapshot():
        latest = (store.get_run(run_id) if run_id
                  else store.latest_finished_run(site_key))
        selected_site = site_key or (latest["site_key"] if latest else "")
        ledger = store.ledger_counts(selected_site)
        runs = [_run_payload(run) for run in store.list_runs(selected_site, limit=20)]
        jobs = [_job_payload(row) for row in store.list_jobs(site_key=selected_site, limit=10000)]
        events: list[dict[str, Any]] = []
        counts = {name: 0 for name in EVENT_TYPES}
        evidence: dict[str, Any] | None = None
        evidence_digest: str | None = None
        raw_count = 0
        if latest:
            counts = store.event_counts(latest["run_id"])
            events = [_event_payload(row)
                      for row in store.list_events(run_id=latest["run_id"], limit=10000)]
            evidence, evidence_digest = _run_evidence(store, latest)
            raw_count = len(store.raw_documents_for_run(latest["run_id"]))
        jobs_digest = sha256_text(canonical_json(jobs))
    summary = {
        "site_key": selected_site, "jobs_total": ledger["total"],
        "jobs_open": ledger.get("open", 0), "jobs_closed": ledger.get("closed", 0),
        "run_id": latest["run_id"] if latest else None,
        "run_status": latest["status"] if latest else "none",
        "run_complete": bool(latest["is_complete"]) if latest else False,
        "run_scenario": latest["scenario"] if latest else None,
        "run_started_at": _timestamp(latest["started_at"]) if latest else None,
        "run_finished_at": _timestamp(latest["finished_at"]) if latest else None,
        "event_counts": counts,
    }
    latest_payload = _run_payload(latest) if latest else None
    if latest_payload:
        notes = store.notes(latest)
        latest_payload.update(
            {key: notes.get(key, {} if key == "selector_misses" else [])
             for key in ("lifecycle", "failures", "warnings", "selector_misses")}
        )
    report = {
        "schema_version": 1, "generated_at": iso_z(clock.now()),
        "site_key": selected_site, "summary": summary,
        "latest_run": latest_payload, "runs": runs, "jobs": jobs, "events": events,
        "raw_evidence": {"documents_in_latest_run": raw_count},
        "bundle": {
            "schema_version": 1, "run_id": summary["run_id"],
            "run_started_at": summary["run_started_at"],
            "run_finished_at": summary["run_finished_at"],
            "run_complete": summary["run_complete"],
            "observation_complete": summary["run_complete"],
            "observation_scope": "complete" if summary["run_complete"] else "incomplete",
            "run_evidence_digest": evidence_digest,
            "run_evidence": evidence, "jobs_digest": jobs_digest,
            "jobs_count": len(jobs), "events_count": len(events),
            "ledger_counts": dict(ledger), "raw_count": raw_count,
            "bundle_id": None, "content_digest": None,
        },
    }
    bundle_id, digest = compute_identity(report)
    report["bundle"]["bundle_id"] = bundle_id
    report["bundle"]["content_digest"] = digest
    return report


def write_reports(
    store: Store, output_dir: str | Path, clock: Clock, site_key: str | None = None,
    *, run_id: str | None = None,
) -> dict[str, str]:
    """Publish one verified four-format bundle and return the bundle paths."""

    from .publication import publish_report

    return publish_report(store, output_dir, clock, site_key, run_id=run_id)["paths"]
