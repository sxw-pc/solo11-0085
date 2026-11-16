"""Offline historical correction entry for late-arriving collection batches.

A correction is always explicit: it binds a correction batch identity to the
run it corrects, the business date it belongs to, and a source summary. Two
caller-selected authorization semantics are supported: ``archive`` stores the
corrected observations and the diff without touching current state or published
events; ``recompute`` replays complete batches in business-time order from the
corrected date on, replacing affected derived events and current job state while
keeping the old conclusions in ``derived_archive`` for review.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import CorrectionError
from .fingerprint import canonical_json, content_hash, job_uid
from .models import JobObservation
from .replay import (
    ReplayBatch,
    observation_view,
    payload_view,
    replay_timeline,
    tracked_from_payload,
    values_from_payload,
    write_job_state,
)
from .store import Store

ARCHIVE = "archive"
RECOMPUTE = "recompute"
MODES = (ARCHIVE, RECOMPUTE)
SEMANTICS = {
    ARCHIVE: "证据归档：仅保存更正观察与差异，不改变当前岗位状态与已发布事件",
    RECOMPUTE: "生命周期重算：按业务时点重放自被更正日期起的完整批次，"
    "替换受影响的派生事件与当前状态，旧结论归档可追溯",
}


@dataclass
class CorrectionResult:
    correction_id: str
    run_id: str
    corrected_run_id: str
    site_key: str
    business_date: str
    mode: str
    status: str
    semantics: str
    source_summary: str
    diff: dict[str, list[str]] = field(default_factory=dict)
    affected_run_ids: list[str] = field(default_factory=list)
    events_archived: int = 0
    events_emitted: int = 0
    ledger: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "correction_id": self.correction_id, "run_id": self.run_id,
            "corrected_run_id": self.corrected_run_id, "site_key": self.site_key,
            "business_date": self.business_date, "mode": self.mode,
            "status": self.status, "semantics": self.semantics,
            "source_summary": self.source_summary, "diff": self.diff,
            "affected_run_ids": list(self.affected_run_ids),
            "events_archived": self.events_archived,
            "events_emitted": self.events_emitted, "ledger": dict(self.ledger),
        }


def _batch_from_run(store: Store, run: dict[str, Any]) -> ReplayBatch:
    views = []
    for row in store.run_observations(run["run_id"]):
        payload = json.loads(row["payload_json"])
        views.append({
            "external_id": row["external_id"], "title": payload.get("title") or "",
            "tracked": tracked_from_payload(payload), "content_hash": row["content_hash"],
            "values": values_from_payload(payload),
        })
    return ReplayBatch(
        run_id=run["run_id"], business_date=run["business_date"] or "",
        complete=bool(run["is_complete"]), observed_at=run["started_at"],
        observations=views,
    )


def _diff(original: dict[str, str], corrected: dict[str, str]) -> dict[str, list[str]]:
    return {
        "appeared": sorted(set(corrected) - set(original)),
        "vanished": sorted(set(original) - set(corrected)),
        "changed": sorted(
            key for key in set(original) & set(corrected)
            if original[key] != corrected[key]
        ),
    }


def submit_correction(
    store: Store, *, correction_id: str, corrected_run_id: str, business_date: str,
    source_summary: str, mode: str,
    observations: list[JobObservation | dict[str, Any]],
    now: str, close_after_missing_runs: int = 2,
    hook: Callable[[str], None] | None = None,
) -> CorrectionResult:
    """Apply one explicit historical correction; idempotent per correction_id.

    ``observations`` accepts JobObservation objects or stored content payloads
    (for example snapshots of an earlier, trusted batch).
    """
    if mode not in MODES:
        raise CorrectionError(f"unknown correction mode {mode!r}; expected one of {MODES}")
    if not correction_id or not source_summary:
        raise CorrectionError("correction_id and source_summary must not be empty")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", business_date):
        raise CorrectionError(f"business_date must be an ISO date (YYYY-MM-DD): {business_date!r}")
    corrected = store.get_run(corrected_run_id)
    if corrected is None:
        raise CorrectionError(f"corrected run not found: {corrected_run_id}")
    if not corrected["is_complete"]:
        raise CorrectionError(f"corrected run is not a complete run: {corrected_run_id}")
    if corrected["business_date"] != business_date:
        raise CorrectionError(
            f"business date {business_date} does not match corrected run "
            f"{corrected_run_id} ({corrected['business_date'] or 'no business date'})"
        )
    site_key = corrected["site_key"]
    views = [
        observation_view(item) if isinstance(item, JobObservation) else payload_view(item)
        for item in observations
    ]
    if not views:
        raise CorrectionError("correction batch must contain at least one observation")
    fingerprint = content_hash({
        "corrected_run_id": corrected_run_id, "business_date": business_date,
        "mode": mode, "source_summary": source_summary,
        "observations": sorted((view["external_id"], view["content_hash"]) for view in views),
    })
    existing = store.get_correction(correction_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise CorrectionError(
                f"correction {correction_id} already recorded with a different payload"
            )
        return CorrectionResult(
            correction_id=correction_id, run_id=existing["run_id"],
            corrected_run_id=corrected_run_id, site_key=site_key,
            business_date=business_date, mode=mode, status="duplicate",
            semantics=existing["semantics"], source_summary=source_summary,
            diff=json.loads(existing["diff_json"] or "{}"),
            ledger=store.ledger_counts(site_key),
        )
    if store.get_run(correction_id) is not None:
        raise CorrectionError(f"correction_id collides with an existing run: {correction_id}")
    corrections = store.list_corrections(site_key)
    if mode == RECOMPUTE and corrected_run_id in {
        row["corrected_run_id"] for row in corrections if row["mode"] == RECOMPUTE
    }:
        raise CorrectionError(
            f"run {corrected_run_id} is already superseded by a recompute correction; "
            "correct the latest correction batch instead"
        )
    original = {
        row["external_id"]: row["content_hash"]
        for row in store.run_observations(corrected_run_id)
    }
    corrected_hashes: dict[str, str] = {}
    for view in views:
        corrected_hashes.setdefault(view["external_id"], view["content_hash"])
    diff = _diff(original, corrected_hashes)
    batches: list[ReplayBatch] = []
    affected: list[str] = []
    replaced_run_ids: list[str] = []
    events_by_run: dict[str, list[tuple[Any, ...]]] = {}
    ledger: dict[str, Any] = {}
    if mode == RECOMPUTE:
        timeline = store.timeline_runs(site_key)
        replaced = False
        for run in timeline:
            if run["run_id"] == corrected_run_id:
                batches.append(ReplayBatch(
                    run_id=correction_id, business_date=business_date, complete=True,
                    observed_at=now, observations=views,
                ))
                replaced = True
            else:
                batches.append(_batch_from_run(store, run))
        if not replaced:
            raise CorrectionError(
                f"corrected run {corrected_run_id} is not part of the effective timeline"
            )
        complete_dates: dict[str, int] = {}
        for batch in batches:
            if batch.complete:
                complete_dates[batch.business_date] = complete_dates.get(batch.business_date, 0) + 1
        conflicts = sorted(date for date, count in complete_dates.items() if count > 1)
        if conflicts:
            raise CorrectionError(
                f"business date(s) {', '.join(conflicts)} hold multiple complete batches "
                "without a correction relationship; refusing to auto-adjudicate"
            )
        events_by_run, ledger = replay_timeline(
            batches=batches, close_after_missing_runs=close_after_missing_runs
        )
        affected = [batch.run_id for batch in batches if batch.business_date >= business_date]
        replaced_run_ids = [corrected_run_id] + [
            run_id for run_id in affected if run_id != correction_id
        ]
    archived = emitted = 0
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO runs(run_id, site_key, scenario, base_url, config_hash, started_at,
                finished_at, status, is_complete, unique_jobs, business_date, notes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', 1, ?, ?, ?)""",
            (correction_id, site_key, f"correction:{mode}", corrected["base_url"],
             corrected["config_hash"], now, now, len(corrected_hashes), business_date,
             canonical_json({"correction_id": correction_id,
                             "corrected_run_id": corrected_run_id,
                             "source_summary": source_summary})),
        )
        for view in views:
            conn.execute(
                """INSERT OR IGNORE INTO job_snapshots(
                    job_uid, run_id, content_hash, payload_json, observed_at)
                VALUES (?, ?, ?, ?, ?)""",
                (job_uid(site_key, view["external_id"]), correction_id,
                 view["content_hash"], canonical_json(view["payload"]), now),
            )
        conn.execute(
            """INSERT INTO corrections(correction_id, site_key, run_id, corrected_run_id,
                business_date, mode, source_summary, fingerprint, status, semantics,
                diff_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?, ?)""",
            (correction_id, site_key, correction_id, corrected_run_id, business_date,
             mode, source_summary, fingerprint, SEMANTICS[mode], canonical_json(diff), now),
        )
        if mode == RECOMPUTE:
            for run_id in replaced_run_ids:
                rows = conn.execute(
                    "SELECT * FROM events WHERE run_id = ?", (run_id,)
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """INSERT INTO derived_archive(
                            correction_id, kind, ref, payload_json, archived_at)
                        VALUES (?, 'event', ?, ?, ?)""",
                        (correction_id, run_id, canonical_json(dict(row)), now),
                    )
                    archived += 1
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            for row in conn.execute(
                "SELECT * FROM jobs WHERE site_key = ?", (site_key,)
            ).fetchall():
                conn.execute(
                    """INSERT INTO derived_archive(
                        correction_id, kind, ref, payload_json, archived_at)
                    VALUES (?, 'job_state', ?, ?, ?)""",
                    (correction_id, row["job_uid"], canonical_json(dict(row)), now),
                )
            if hook:
                hook("archived")
            for batch in batches:
                if batch.run_id not in affected:
                    continue
                for event_type, external_id, field_name, old, new in events_by_run[batch.run_id]:
                    conn.execute(
                        """INSERT INTO events(
                            job_uid, run_id, event_type, field, old_value, new_value, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (job_uid(site_key, external_id), batch.run_id, event_type, field_name,
                         None if old is None else str(old),
                         None if new is None else str(new), batch.observed_at),
                    )
                    emitted += 1
            if hook:
                hook("events")
            keep = {job_uid(site_key, external_id) for external_id in ledger}
            for row in conn.execute(
                "SELECT job_uid FROM jobs WHERE site_key = ?", (site_key,)
            ).fetchall():
                if row["job_uid"] not in keep:
                    conn.execute("DELETE FROM jobs WHERE job_uid = ?", (row["job_uid"],))
            for job in ledger.values():
                write_job_state(conn, site_key=site_key, job=job, now=now)
            if hook:
                hook("jobs")
    return CorrectionResult(
        correction_id=correction_id, run_id=correction_id,
        corrected_run_id=corrected_run_id, site_key=site_key,
        business_date=business_date, mode=mode, status="applied",
        semantics=SEMANTICS[mode], source_summary=source_summary, diff=diff,
        affected_run_ids=affected, events_archived=archived, events_emitted=emitted,
        ledger=store.ledger_counts(site_key),
    )
