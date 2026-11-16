"""SQLite persistence with explicit transactions and idempotent writes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import StorageError
from .fingerprint import canonical_json
from .rawstore import RawDocument
from .schema import SCHEMA, SCHEMA_VERSION


class Store:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = str(path)
        self.readonly = readonly
        if self.path != ":memory:" and readonly and not Path(self.path).is_file():
            raise StorageError(f"database not found: {self.path}")
        if not readonly and self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            if readonly:
                uri = Path(self.path).resolve().as_uri() + "?mode=ro"
                self.conn = sqlite3.connect(uri, uri=True, timeout=5)
            else:
                self.conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open SQLite database: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if readonly:
            self.conn.execute("PRAGMA query_only = ON")
        else:
            try:
                self.conn.execute("PRAGMA journal_mode = WAL")
                self.conn.executescript(SCHEMA)
                self._migrate()
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            except sqlite3.Error as exc:
                raise StorageError(f"cannot initialize database: {exc}") from exc

    def _migrate(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
        if "business_date" not in columns:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN business_date TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise StorageError("cannot write through a read-only store")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def begin_run(self, *, run_id: str, site_key: str, scenario: str, base_url: str,
                  config_hash: str, started_at: str, resumed: bool = False,
                  business_date: str = "") -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO runs(
                    run_id, site_key, scenario, base_url, config_hash, started_at,
                    business_date, resumed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, site_key, scenario, base_url, config_hash, started_at,
                 business_date, int(resumed)),
            )
            return cursor.rowcount == 1

    def finish_run(self, run_id: str, *, status: str, is_complete: bool, finished_at: str,
                   pages_fetched: int = 0, list_cards: int = 0, unique_jobs: int = 0,
                   details_ok: int = 0, details_failed: int = 0, duplicate_ids: int = 0,
                   parse_warnings: int = 0, stop_reason: str | None = None,
                   notes: dict[str, Any] | None = None, resumed: bool = False) -> None:
        if status not in {"complete", "incomplete", "failed"}:
            raise StorageError(f"invalid run status: {status}")
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE runs SET finished_at=?, status=?, is_complete=?, pages_fetched=?,
                    list_cards=?, unique_jobs=?, details_ok=?, details_failed=?,
                    duplicate_ids=?, parse_warnings=?, resumed=?, stop_reason=?, notes_json=?
                WHERE run_id=?""",
                (finished_at, status, int(is_complete), pages_fetched, list_cards,
                 unique_jobs, details_ok, details_failed, duplicate_ids, parse_warnings,
                 int(resumed), stop_reason, canonical_json(notes or {}), run_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"run was not started: {run_id}")

    def run_is_finished(self, run_id: str) -> bool:
        row = self.conn.execute("SELECT finished_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return bool(row and row["finished_at"])

    def record_raw_document(self, document: RawDocument) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO raw_documents(sha256, size_bytes, content_type, first_seen_at)
                VALUES (?, ?, ?, ?)""",
                (document.sha256, document.size_bytes, document.content_type, document.fetched_at),
            )
            conn.execute(
                """INSERT OR IGNORE INTO run_documents(
                    run_id, sha256, url, kind, http_status, fetched_at, relative_path, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (document.run_id, document.sha256, document.url, document.kind,
                 document.http_status, document.fetched_at, document.relative_path,
                 document.size_bytes),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run(self, site_key: str | None = None) -> dict[str, Any] | None:
        sql, params = "SELECT * FROM runs", ()
        if site_key:
            sql, params = sql + " WHERE site_key = ?", (site_key,)
        row = self.conn.execute(sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None

    def list_runs(self, site_key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM runs", []
        if site_key:
            sql, params = sql + " WHERE site_key = ?", [site_key]
        rows = self.conn.execute(sql + " ORDER BY started_at DESC, rowid DESC LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]

    def timeline_runs(self, site_key: str) -> list[dict[str, Any]]:
        """Lifecycle-applied runs in business-time order with corrections resolved."""
        rows = self.conn.execute(
            """SELECT * FROM runs WHERE site_key = ? AND status IN ('complete', 'incomplete')
            AND run_id NOT IN (SELECT run_id FROM corrections WHERE mode = 'archive')
            AND run_id NOT IN (
                SELECT corrected_run_id FROM corrections WHERE mode = 'recompute')
            ORDER BY business_date, started_at, rowid""",
            (site_key,),
        )
        return [dict(row) for row in rows]

    def run_observations(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT j.external_id, s.content_hash, s.payload_json
            FROM job_snapshots s JOIN jobs j ON j.job_uid = s.job_uid
            WHERE s.run_id = ? ORDER BY j.external_id""",
            (run_id,),
        )
        return [dict(row) for row in rows]

    def get_correction(self, correction_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM corrections WHERE correction_id = ?", (correction_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_corrections(self, site_key: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM corrections", ()
        if site_key:
            sql, params = sql + " WHERE site_key = ?", (site_key,)
        rows = self.conn.execute(sql + " ORDER BY created_at, rowid", params)
        return [dict(row) for row in rows]

    def get_job(self, job_uid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_uid = ?", (job_uid,)).fetchone()
        return dict(row) if row else None

    def find_job(self, site_key: str, external_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE site_key = ? AND external_id = ?", (site_key, external_id)
        ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, *, site_key: str | None = None, status: str | None = None,
                  limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = [], []
        if site_key:
            clauses.append("site_key = ?")
            params.append(site_key)
        if status:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        rows = self.conn.execute(sql + " ORDER BY external_id LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]

    def list_snapshots(self, job_uid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT job_uid, run_id, content_hash, payload_json, observed_at
            FROM job_snapshots WHERE job_uid = ? ORDER BY observed_at, run_id""",
            (job_uid,),
        )
        return [dict(row) for row in rows]

    def list_events(self, *, run_id: str | None = None, job_uid: str | None = None,
                    limit: int = 1000) -> list[dict[str, Any]]:
        clauses, params = [], []
        if run_id:
            clauses.append("e.run_id = ?")
            params.append(run_id)
        if job_uid:
            clauses.append("e.job_uid = ?")
            params.append(job_uid)
        sql = """SELECT e.*, j.external_id, j.title, j.company
            FROM events e JOIN jobs j ON j.job_uid = e.job_uid"""
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(sql + " ORDER BY e.created_at, e.id LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]

    def event_counts(self, run_id: str) -> dict[str, int]:
        counts = {"NEW": 0, "CHANGED": 0, "DISAPPEARED": 0, "CLOSED": 0, "REOPENED": 0}
        rows = self.conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM events WHERE run_id = ? GROUP BY event_type",
            (run_id,),
        )
        for row in rows:
            counts[row["event_type"]] = row["n"]
        return counts

    def ledger_counts(self, site_key: str | None = None) -> dict[str, int]:
        counts = {"total": 0, "open": 0, "closed": 0}
        sql, params = "SELECT status, COUNT(*) AS n FROM jobs", ()
        if site_key:
            sql, params = sql + " WHERE site_key = ?", (site_key,)
        for row in self.conn.execute(sql + " GROUP BY status", params):
            counts[row["status"]] = row["n"]
            counts["total"] += row["n"]
        return counts

    def raw_documents_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM run_documents WHERE run_id = ? ORDER BY url, sha256", (run_id,)
        )
        return [dict(row) for row in rows]

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def notes(self, run: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(run.get("notes_json") or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}
