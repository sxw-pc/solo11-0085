"""Pure business-time replay of collection batches for lifecycle recomputation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .fingerprint import TRACKED_FIELDS, content_hash, job_uid
from .lifecycle import _JOB_COLUMNS, _JOB_PLACEHOLDERS, _observation_values
from .models import JobObservation


def tracked_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "title": payload.get("title") or "", "company": payload.get("company") or "",
        "location": payload.get("location_raw") or "",
        "salary": payload.get("salary_text") or "",
        "experience": payload.get("experience_text") or "",
        "employment_type": payload.get("employment_type") or "",
        "tags": "、".join(str(tag) for tag in (payload.get("tags") or [])),
    }


def values_from_payload(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload.get("external_id") or "", payload.get("title") or "",
        payload.get("company") or "", payload.get("location_raw") or "",
        payload.get("city"), payload.get("district"),
        payload.get("salary_min"), payload.get("salary_max"),
        payload.get("salary_text") or "", payload.get("salary_currency") or "CNY",
        payload.get("salary_period") or "month", payload.get("salary_months") or 12,
        int(bool(payload.get("salary_negotiable"))),
        payload.get("experience_text"), payload.get("experience_min"),
        payload.get("experience_max"), payload.get("employment_type"),
        payload.get("description") or "",
        json.dumps(list(payload.get("tags") or []), ensure_ascii=False),
        payload.get("detail_url") or "", payload.get("posted_at"),
        payload.get("source_status"),
    )


def observation_view(observation: JobObservation) -> dict[str, Any]:
    """Batch-independent view of one observation used by replay and corrections."""
    return {
        "external_id": observation.external_id, "title": observation.title,
        "tracked": observation.tracked_values(), "content_hash": observation.content_hash,
        "values": _observation_values(observation), "payload": observation.content_payload(),
    }


def payload_view(payload: dict[str, Any]) -> dict[str, Any]:
    """View rebuilt from a stored content payload (e.g. archived evidence)."""
    return {
        "external_id": payload.get("external_id") or "",
        "title": payload.get("title") or "",
        "tracked": tracked_from_payload(payload), "content_hash": content_hash(payload),
        "values": values_from_payload(payload), "payload": payload,
    }


@dataclass
class ReplayBatch:
    run_id: str
    business_date: str
    complete: bool
    observed_at: str
    observations: list[dict[str, Any]]


@dataclass
class ReplayJob:
    external_id: str
    tracked: dict[str, str]
    values: tuple[Any, ...]
    content_hash: str
    status: str = "open"
    missing_streak: int = 0
    reopen_count: int = 0
    closed_at: str | None = None
    first_seen_run_id: str = ""
    first_seen_at: str = ""
    last_seen_run_id: str = ""
    last_seen_at: str = ""


def replay_timeline(*, batches: list[ReplayBatch], close_after_missing_runs: int
                    ) -> tuple[dict[str, list[tuple[Any, ...]]], dict[str, ReplayJob]]:
    """Re-derive per-run events and final job states from ordered batches.

    Mirrors LifecycleEngine.apply semantics; incomplete batches never count as
    missing evidence. Events are (event_type, external_id, field, old, new).
    """
    events: dict[str, list[tuple[Any, ...]]] = {}
    ledger: dict[str, ReplayJob] = {}
    for batch in batches:
        run_events: list[tuple[Any, ...]] = []
        unique: dict[str, dict[str, Any]] = {}
        for view in batch.observations:
            unique.setdefault(view["external_id"], view)
        seen: set[str] = set()
        for external_id in sorted(unique):
            view = unique[external_id]
            seen.add(external_id)
            job = ledger.get(external_id)
            if job is None:
                job = ReplayJob(
                    external_id=external_id, tracked=view["tracked"],
                    values=view["values"], content_hash=view["content_hash"],
                    first_seen_run_id=batch.run_id, first_seen_at=batch.observed_at,
                )
                ledger[external_id] = job
                run_events.append(("NEW", external_id, "job", None, view["title"]))
            else:
                if job.status == "closed":
                    job.reopen_count += 1
                    run_events.append(("REOPENED", external_id, "status", "closed", "open"))
                for field_name in TRACKED_FIELDS:
                    old = job.tracked.get(field_name, "")
                    new = view["tracked"].get(field_name, "")
                    if old != new:
                        run_events.append(("CHANGED", external_id, field_name, old, new))
                job.status, job.missing_streak, job.closed_at = "open", 0, None
                job.tracked = view["tracked"]
                job.values, job.content_hash = view["values"], view["content_hash"]
            job.last_seen_run_id, job.last_seen_at = batch.run_id, batch.observed_at
        if batch.complete:
            for external_id, job in ledger.items():
                if external_id in seen or job.status != "open":
                    continue
                job.missing_streak += 1
                run_events.append(("DISAPPEARED", external_id, "presence", "seen",
                                   f"missing_{job.missing_streak}"))
                if job.missing_streak >= close_after_missing_runs:
                    job.status, job.closed_at = "closed", batch.observed_at
                    run_events.append(("CLOSED", external_id, "status", "open", "closed"))
        events[batch.run_id] = run_events
    return events, ledger


def write_job_state(conn: Any, *, site_key: str, job: ReplayJob, now: str) -> None:
    uid = job_uid(site_key, job.external_id)
    columns = [part.strip() for part in _JOB_COLUMNS.strip().split(",")]
    assignments = ", ".join(f"{column} = ?" for column in columns)
    state = (job.status, job.missing_streak, job.reopen_count, job.first_seen_run_id,
             job.first_seen_at, job.last_seen_run_id, job.last_seen_at, job.closed_at, now)
    if conn.execute("SELECT 1 FROM jobs WHERE job_uid = ?", (uid,)).fetchone():
        conn.execute(
            f"""UPDATE jobs SET {assignments}, status = ?, missing_streak = ?,
                reopen_count = ?, first_seen_run_id = ?, first_seen_at = ?,
                last_seen_run_id = ?, last_seen_at = ?, closed_at = ?, updated_at = ?
            WHERE job_uid = ?""",
            (*job.values, job.content_hash, *state, uid),
        )
    else:
        conn.execute(
            f"""INSERT INTO jobs(job_uid, site_key, {_JOB_COLUMNS}, status, missing_streak,
                reopen_count, first_seen_run_id, first_seen_at, last_seen_run_id,
                last_seen_at, closed_at, updated_at)
            VALUES (?, ?, {_JOB_PLACEHOLDERS}, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, site_key, *job.values, job.content_hash, *state),
        )
