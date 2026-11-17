"""Content-addressed raw HTML evidence and manifest integrity."""
from __future__ import annotations
import hashlib
from jobtrace.clock import FixedClock, parse_instant
from jobtrace.rawstore import RawStore
from jobtrace.store import Store
def test_every_crawl_response_is_hashed_and_manifested(collect):
    loaded, outcome = collect("baseline", run_id="run-evidence")
    root = loaded.config.storage.raw_dir
    store = RawStore(root, FixedClock(parse_instant("2026-01-15T09:00:00Z")))
    records = list(store.iter_manifest())
    assert len(records) == 71
    assert {record.kind for record in records} == {"list", "detail"}
    assert sum(record.kind == "list" for record in records) == 7
    assert all(record.url.startswith("http://127.0.0.1:") for record in records)
    for record in records:
        content = store.read(record.sha256)
        assert hashlib.sha256(content).hexdigest() == record.sha256
        assert store.verify(record)
    detail = next(record for record in records if record.url.endswith("/jobs/JT-0001"))
    assert "合成数据" in store.read(detail.sha256).decode("utf-8")
    db = Store(loaded.config.storage.database)
    try:
        assert len(db.raw_documents_for_run("run-evidence")) == len(outcome.result.documents) == 71
    finally:
        db.close()
def test_duplicate_content_is_stored_once_but_manifested_per_fetch(tmp_path):
    store = RawStore(tmp_path / "raw", FixedClock(parse_instant("2026-01-15T09:00:00Z")))
    content = b"<html><body>same synthetic response</body></html>"
    first = store.put(content, url="http://127.0.0.1:8765/jobs?page=1", kind="list", run_id="r1")
    second = store.put(content, url="http://127.0.0.1:8765/jobs?page=1", kind="list", run_id="r2")
    assert first.sha256 == second.sha256
    assert first.relative_path == second.relative_path
    assert len(list(store.iter_manifest())) == 2
    objects = [path for path in (tmp_path / "raw" / "objects").rglob("*") if path.is_file()]
    assert len(objects) == 1
    assert store.read(first.sha256) == content
