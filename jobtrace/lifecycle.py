"""Lifecycle event calculation for the job ledger."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any
from .fingerprint import TRACKED_FIELDS, canonical_json, job_uid
from .impact import compare_requirements
from .models import JobObservation
from .store import Store
@dataclass
class LifecycleResult:
    new_jobs: int = 0
    changed_events: int = 0
    disappeared: int = 0
    closed: int = 0
    reopened: int = 0
    unchanged: int = 0
    skipped: bool = False
    changed_fields: dict[str, int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    # Derived-only evidence: description requirement impact. Never emits events
    # and never mutates content_hash; the raw descriptions stay in snapshots.
    requirement_impacts: list[dict[str, Any]] = field(default_factory=list)
    def summary(self) -> dict[str, Any]:
        return {
            "new_jobs": self.new_jobs, "changed_events": self.changed_events,
            "disappeared": self.disappeared, "closed": self.closed,
            "reopened": self.reopened, "unchanged": self.unchanged,
            "skipped": self.skipped,
            "changed_fields": dict(sorted(self.changed_fields.items())),
        }
def _event(conn: Any, *, job_uid_value: str, run_id: str, event_type: str,
           field_name: str, old: Any, new: Any, created_at: str) -> bool:
    cursor = conn.execute(
        """INSERT OR IGNORE INTO events(
            job_uid, run_id, event_type, field, old_value, new_value, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_uid_value, run_id, event_type, field_name,
         None if old is None else str(old), None if new is None else str(new), created_at),
    )
    return cursor.rowcount == 1
def _row_tracked(row: dict[str, Any]) -> dict[str, str]:
    try:
        tags = "、".join(json.loads(row.get("tags_json") or "[]"))
    except (TypeError, json.JSONDecodeError):
        tags = ""
    return {
        "title": row.get("title") or "", "company": row.get("company") or "",
        "location": row.get("location_raw") or "", "salary": row.get("salary_text") or "",
        "experience": row.get("experience_text") or "",
        "employment_type": row.get("employment_type") or "", "tags": tags,
    }
def _observation_values(observation: JobObservation) -> tuple[Any, ...]:
    salary = observation.salary
    return (
        observation.external_id, observation.title, observation.company,
        observation.location_raw, observation.city, observation.district,
        salary.min_monthly, salary.max_monthly, salary.normalized, salary.currency,
        salary.source_period, salary.months_per_year, int(salary.negotiable),
        observation.experience.text, observation.experience.min_years,
        observation.experience.max_years, observation.employment_type,
        observation.description, json.dumps(list(observation.tags), ensure_ascii=False),
        observation.detail_url, observation.posted_at, observation.source_status,
    )
_JOB_COLUMNS = """
    external_id, title, company, location_raw, city, district,
    salary_min, salary_max, salary_text, salary_currency, salary_period,
    salary_months, salary_negotiable, experience_text, experience_min,
    experience_max, employment_type, description, tags_json, detail_url,
    posted_at, source_status, content_hash
"""
_JOB_PLACEHOLDERS = ", ".join("?" for _ in range(23))
class LifecycleEngine:
    def __init__(self, store: Store): self.store = store
    def apply(self, *, run_id: str, site_key: str, observations: list[JobObservation],
              complete: bool, now: str, close_after_missing_runs: int) -> LifecycleResult:
        if self.store.run_is_finished(run_id): return LifecycleResult(skipped=True)
        result = LifecycleResult()
        unique: dict[str, JobObservation] = {}
        for observation in observations:
            unique.setdefault(observation.external_id, observation)
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE site_key = ?", (site_key,)).fetchall()
            existing = {row["external_id"]: dict(row) for row in rows}
            observed_ids: set[str] = set()
            for external_id in sorted(unique):
                observation = unique[external_id]
                observed_ids.add(external_id)
                payload_json = canonical_json(observation.content_payload())
                digest = observation.content_hash
                row = existing.get(external_id)
                values = _observation_values(observation)
                if row is None:
                    uid = job_uid(site_key, external_id)
                    _insert_job(conn, uid, site_key, run_id, now, digest, values)
                    if _event(conn, job_uid_value=uid, run_id=run_id, event_type="NEW",
                              field_name="job", old=None, new=observation.title, created_at=now):
                        result.new_jobs += 1
                else:
                    uid = row["job_uid"]
                    reopened = False
                    if row["status"] == "closed":
                        reopened = _event(
                            conn, job_uid_value=uid, run_id=run_id, event_type="REOPENED",
                            field_name="status", old="closed", new="open", created_at=now,
                        )
                        if reopened:
                            result.reopened += 1
                            conn.execute(
                                "UPDATE jobs SET reopen_count = reopen_count + 1 WHERE job_uid = ?",
                                (uid,),
                            )
                    old_values = _row_tracked(row)
                    new_values = observation.tracked_values()
                    changed = 0
                    for field_name in TRACKED_FIELDS:
                        if old_values.get(field_name, "") == new_values.get(field_name, ""):
                            continue
                        if _event(conn, job_uid_value=uid, run_id=run_id, event_type="CHANGED",
                                  field_name=field_name, old=old_values.get(field_name, ""),
                                  new=new_values.get(field_name, ""), created_at=now):
                            changed += 1
                            result.changed_events += 1
                            key = result.changed_fields
                            key[field_name] = key.get(field_name, 0) + 1
                    if not changed and not reopened: result.unchanged += 1
                    if row.get("description", "") != observation.description:
                        result.requirement_impacts.append(
                            self._requirement_impact(uid, row, observation, run_id, now))
                    _update_job(conn, uid, run_id, now, digest, values)
                self._snapshot(conn, uid, run_id, digest, payload_json, now)
            if complete:
                self._handle_missing(conn, rows=rows, observed=observed_ids, run_id=run_id,
                                     now=now, threshold=close_after_missing_runs, result=result)
        if (result.new_jobs or result.changed_events or result.disappeared
                or result.closed or result.reopened):
            result.events = self.store.list_events(run_id=run_id, limit=10000)
        return result
    def _snapshot(self, conn: Any, uid: str, run_id: str, digest: str,
                  payload_json: str, now: str) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO job_snapshots(
                job_uid, run_id, content_hash, payload_json, observed_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (uid, run_id, digest, payload_json, now),
        )
    def _requirement_impact(self, uid: str, row: dict[str, Any],
                            observation: JobObservation, run_id: str,
                            now: str) -> dict[str, Any]:
        # Recomputed from raw text only; ruleset version changes never rewrite
        # the persisted description, content_hash, or lifecycle events.
        comparison = compare_requirements(row.get("description") or "", observation.description)
        return {"job_uid": uid, "external_id": observation.external_id, "run_id": run_id,
                "observed_at": now, "old_run_id": row.get("last_seen_run_id"),
                "text_revised": True, "impact": comparison.as_dict()}
    def _handle_missing(self, conn: Any, *, rows: list[Any], observed: set[str],
                        run_id: str, now: str, threshold: int, result: LifecycleResult) -> None:
        for raw_row in rows:
            row = dict(raw_row)
            if row["external_id"] in observed or row["status"] != "open": continue
            streak = int(row["missing_streak"]) + 1
            if _event(conn, job_uid_value=row["job_uid"], run_id=run_id,
                      event_type="DISAPPEARED", field_name="presence", old="seen",
                      new=f"missing_{streak}", created_at=now):
                result.disappeared += 1
            status = "closed" if streak >= threshold else "open"
            closed_at = now if status == "closed" else None
            if status == "closed" and _event(
                conn, job_uid_value=row["job_uid"], run_id=run_id, event_type="CLOSED",
                field_name="status", old="open", new="closed", created_at=now,
            ):
                result.closed += 1
            conn.execute(
                """UPDATE jobs SET missing_streak = ?, status = ?, closed_at = ?, updated_at = ?
                WHERE job_uid = ?""",
                (streak, status, closed_at, now, row["job_uid"]),
            )
def _insert_job(conn: Any, uid: str, site_key: str, run_id: str, now: str,
                digest: str, values: tuple[Any, ...]) -> None:
    conn.execute(
        f"""INSERT INTO jobs(
            job_uid, site_key, {_JOB_COLUMNS}, status, missing_streak,
            first_seen_run_id, first_seen_at, last_seen_run_id, last_seen_at, updated_at
        ) VALUES (?, ?, {_JOB_PLACEHOLDERS}, 'open', 0, ?, ?, ?, ?, ?)""",
        (uid, site_key, *values, digest, run_id, now, run_id, now, now),
    )
def _update_job(conn: Any, uid: str, run_id: str, now: str, digest: str,
                values: tuple[Any, ...]) -> None:
    columns = [part.strip() for part in _JOB_COLUMNS.strip().split(",")]
    assignments = ", ".join(f"{column} = ?" for column in columns)
    conn.execute(
        f"""UPDATE jobs SET {assignments}, status = 'open', missing_streak = 0,
            closed_at = NULL, last_seen_run_id = ?, last_seen_at = ?, updated_at = ?
        WHERE job_uid = ?""",
        (*values, digest, run_id, now, now, uid),
    )
