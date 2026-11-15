"""Read-only local review console for the job ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template

from ..errors import StorageError
from ..store import Store


def _open(path: str) -> Store | None:
    try:
        return Store(path, readonly=True)
    except StorageError:
        return None


def _tags(row: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(row.get("tags_json") or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _snapshots(store: Store, job_uid: str) -> list[dict[str, Any]]:
    return [{key: row[key] for key in ("run_id", "content_hash", "observed_at")}
            for row in store.list_snapshots(job_uid)]


def create_app(database: str | Path) -> Flask:
    app = Flask(__name__)
    db_path = str(Path(database).resolve())

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:"
        )
        return response

    @app.get("/healthz")
    def healthz():
        store = _open(db_path)
        if store is None:
            return jsonify({"status": "not_ready"}), 503
        try:
            run = store.latest_run()
            return jsonify({"status": "ok", "latest_run": run["run_id"] if run else None})
        finally:
            store.close()

    @app.get("/")
    def index():
        store = _open(db_path)
        if store is None:
            return render_template("not_ready.html"), 503
        try:
            latest = store.latest_run()
            context = {"counts": store.ledger_counts(), "latest": latest,
                       "lifecycle": store.notes(latest) if latest else {},
                       "events": store.list_events(limit=30),
                       "jobs": store.list_jobs(limit=1000), "runs": store.list_runs(limit=10)}
        finally:
            store.close()
        return render_template("index.html", **context)

    @app.get("/jobs/<job_uid>")
    def job_page(job_uid: str):
        store = _open(db_path)
        if store is None:
            return render_template("not_ready.html"), 503
        try:
            job = store.get_job(job_uid)
            if job is None:
                abort(404)
            events = store.list_events(job_uid=job_uid, limit=500)
            snapshots = _snapshots(store, job_uid)
        finally:
            store.close()
        job["tags"] = _tags(job)
        job["salary_negotiable"] = bool(job["salary_negotiable"])
        return render_template("job.html", job=job, events=events, snapshots=snapshots)

    @app.get("/events")
    def events_page():
        store = _open(db_path)
        if store is None:
            return render_template("not_ready.html"), 503
        try:
            events = store.list_events(limit=1000)
            runs = store.list_runs(limit=20)
        finally:
            store.close()
        return render_template("events.html", events=events, runs=runs)

    @app.get("/runs")
    def runs_page():
        store = _open(db_path)
        if store is None:
            return render_template("not_ready.html"), 503
        try:
            runs = store.list_runs(limit=50)
        finally:
            store.close()
        return render_template("runs.html", runs=runs)

    return app
