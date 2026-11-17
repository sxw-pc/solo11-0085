"""Generate, verify, atomically publish, and recover report bundles."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import report as report_module
from .bundle import (
    BUNDLES_DIR,
    FILENAMES,
    FORMATS,
    MANIFEST,
    PREVIEW_DIR,
    PUBLISHED_POINTER,
    Render,
    ReportBundleError,
    Writes,
    bundle_dir,
    bundle_paths,
    current_bundle,
    digest_file,
    digest_text,
    link_current,
    manifest_formats,
    published_pointer,
    read_json,
    read_preview,
    read_published,
    remove_temporaries,
    verified_formats,
    verify_bundle,
    write_atomic,
    write_manifest,
    write_preview,
)
from .clock import Clock, iso_z
from .fingerprint import canonical_json, sha256_bytes
from .store import Store


def _rank(record: dict[str, Any] | None) -> tuple[str, int]:
    if not record:
        return "", -1
    return (str(record.get("run_started_at") or ""),
            int(record.get("run_rowid") or -1))


def run_still_bound(store: Store, report: dict[str, Any]) -> bool:
    """Check the database still holds the exact run row bound into the report."""

    bundle = report.get("bundle") or {}
    evidence = bundle.get("run_evidence") or {}
    run_id = evidence.get("run_id") or bundle.get("run_id")
    if not run_id:
        return False
    row = store.get_run(run_id)
    if row is None:
        return False
    row = dict(row)
    row["rowid"] = store.scalar("SELECT rowid FROM runs WHERE run_id = ?", (run_id,))
    return sha256_bytes(canonical_json(row).encode("utf-8")) == bundle.get(
        "run_evidence_digest")


def _commit_pointer(output: Path, report: dict[str, Any], manifest: dict[str, Any],
                    store: Store) -> None:
    """Atomically switch ``current`` first, then record the pointer journal.

    A crash between the two steps leaves a committed ``current`` link plus a
    stale journal; recover() re-derives the journal from the verified link.
    """

    _check_order(output, report, store)
    bundle = report["bundle"]
    bundle_id = bundle["bundle_id"]
    link_current(output, bundle_id)
    write_atomic(output / PUBLISHED_POINTER, json.dumps({
        "bundle_id": bundle_id, "run_id": bundle["run_id"],
        "run_started_at": bundle.get("run_started_at"),
        "run_rowid": (bundle.get("run_evidence") or {}).get("rowid"),
        "content_digest": bundle["content_digest"],
        "formats": manifest["formats"],
        "published_at": iso_z(datetime.now(UTC)),
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_manifest(bundle_dir(output, bundle_id), report, manifest["formats"],
                   published=True)


def _check_order(output: Path, report: dict[str, Any], store: Store) -> None:
    bundle = report["bundle"]
    bundle_id = bundle["bundle_id"]
    pointer = published_pointer(output)
    new_rank = _rank({"run_started_at": bundle.get("run_started_at"),
                      "run_rowid": (bundle.get("run_evidence") or {}).get("rowid")})
    same_as_pointer = bool(pointer) and (
        pointer.get("bundle_id") == bundle_id
        and pointer.get("content_digest") == bundle["content_digest"])
    if same_as_pointer:
        return
    if pointer and _rank(pointer) > new_rank:
        raise ReportBundleError(
            f"refusing to move published pointer backwards: {bundle_id}")
    if not run_still_bound(store, report):
        raise ReportBundleError(
            f"run evidence changed since report was read: {bundle['run_id']}")


def _verified_existing(output: Path, bundle_id: str) -> dict[str, Any] | None:
    expected = manifest_formats(output, bundle_id)
    if not expected:
        return None
    folder = bundle_dir(output, bundle_id)
    done, missing = verified_formats(folder, expected)
    if missing or len(done) != len(FORMATS):
        return None
    return verify_bundle(folder, bundle_id)


def _write_candidate(output: Path, report: dict[str, Any], files: dict[str, str],
                     writes: Writes) -> None:
    bundle = report["bundle"]
    folder = bundle_dir(output, bundle["bundle_id"])
    folder.mkdir(parents=True, exist_ok=True)
    record = manifest_formats(output, bundle["bundle_id"])
    for name in FORMATS:
        content = files[name]
        digest = digest_text(content)
        writes.text(folder / FILENAMES[name], content, newline="")
        if digest_file(folder / FILENAMES[name]) != digest:
            raise ReportBundleError(f"{name} digest mismatch after write")
        record = {**record, name: digest}
        write_manifest(folder, report, record)
    write_manifest(folder, report, record)


def publish_report(
    store: Store, output_dir: str | Path, clock: Clock, site_key: str | None = None,
    *, run_id: str | None = None, writes: Writes | None = None,
    render: Render | None = None,
) -> dict[str, Any]:
    """One verified four-format bundle, swapped in atomically via the pointer."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    used = writes if writes is not None else Writes()
    try:
        built = report_module.build_report(store, clock, site_key, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - reported as an explicit status.
        return {"status": "failed", "stage": "build", "bundle_id": None,
                "run_id": run_id, "paths": {}, "error": type(exc).__name__,
                "error_detail": str(exc), "preview": read_preview(output),
                "published": read_published(output)}
    files = (render or report_module.render_bundle)(built)
    identity = built["bundle"]["bundle_id"]
    recent = _verified_existing(output, identity)
    if recent is not None:
        return _settle(output, built, recent, store, "verified identical retry")
    try:
        _write_candidate(output, built, files, used)
        manifest = verify_bundle(bundle_dir(output, identity), identity)
    except Exception as exc:  # noqa: BLE001 - partial candidates become previews.
        write_preview(output, built, "candidate_partial", f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "stage": "write", "bundle_id": identity,
                "run_id": built["bundle"]["run_id"], "paths": {},
                "error": type(exc).__name__, "error_detail": str(exc),
                "preview": read_preview(output, identity),
                "published": read_published(output)}
    return _settle(output, built, manifest, store, "all formats verified")


def _settle(output: Path, report: dict[str, Any], manifest: dict[str, Any],
            store: Store, reason: str) -> dict[str, Any]:
    bundle_id = report["bundle"]["bundle_id"]
    try:
        _commit_pointer(output, report, manifest, store)
    except (ReportBundleError, OSError) as exc:
        # A failed journal write after the link switch is repairable by recover().
        try:
            write_preview(output, report, "candidate_complete", str(exc))
            preview = read_preview(output, bundle_id)
        except OSError:
            preview = None
        return {"status": "held", "stage": "pointer", "bundle_id": bundle_id,
                "run_id": report["bundle"]["run_id"],
                "paths": bundle_paths(output, bundle_id),
                "error": type(exc).__name__, "error_detail": str(exc),
                "preview": preview, "published": read_published(output)}
    write_preview(output, report, "published", reason)
    return {
        "status": "published", "bundle_id": bundle_id,
        "run_id": report["bundle"]["run_id"],
        "paths": bundle_paths(output, bundle_id),
        "published_paths": {name: str(output / FILENAMES[name]) for name in FORMATS},
        "formats": manifest.get("formats", {}),
        "preview": read_preview(output, bundle_id),
        "published": read_published(output),
    }


def recover(output_dir: str | Path, store: Store | None = None) -> dict[str, Any]:
    """Re-verify the committed link, repair the journal, index abandoned bundles."""

    output = Path(output_dir)
    result: dict[str, Any] = {
        "status": "empty", "bundle_id": None, "verified": False,
        "temporaries_removed": remove_temporaries(output), "abandoned": [],
        "repaired": False,
    }
    committed = current_bundle(output)
    if committed:
        try:
            manifest = verify_bundle(bundle_dir(output, committed), committed)
        except (ReportBundleError, OSError, ValueError):
            result.update(status="corrupt", bundle_id=committed)
            return result
        link_current(output, committed)
        pointer = published_pointer(output)
        if not pointer or pointer.get("bundle_id") != committed:
            parsed = read_json(bundle_dir(output, committed) / FILENAMES["json"]) or {}
            bundle = parsed.get("bundle") or {}
            write_atomic(output / PUBLISHED_POINTER, json.dumps({
                "bundle_id": committed, "run_id": manifest.get("run_id"),
                "run_started_at": bundle.get("run_started_at"),
                "run_rowid": (bundle.get("run_evidence") or {}).get("rowid"),
                "content_digest": manifest.get("content_digest"),
                "formats": manifest.get("formats", {}),
                "published_at": iso_z(datetime.now(UTC)),
                "recovered": True,
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            result["repaired"] = True
        result.update(status="published", bundle_id=committed)
        if store is not None:
            parsed = read_json(bundle_dir(output, committed) / FILENAMES["json"])
            result["verified"] = bool(parsed) and run_still_bound(store, parsed)
    for folder in sorted((output / BUNDLES_DIR).iterdir()) if (output / BUNDLES_DIR).is_dir() else []:
        if not folder.is_dir() or folder.name == committed:
            continue
        manifest = read_json(folder / MANIFEST)
        expected = (manifest or {}).get("formats") or {}
        done, missing = (verified_formats(folder, expected) if manifest
                         else ([], list(FORMATS)))
        if not missing and len(done) == len(FORMATS):
            continue
        if (output / PREVIEW_DIR / f"{folder.name}.json").is_file():
            continue
        write_preview(output, {"bundle": {
            "bundle_id": folder.name,
            "content_digest": (manifest or {}).get("content_digest"),
        }}, "candidate_partial", "abandoned bundle recovered from disk")
        result["abandoned"].append(folder.name)
    result["repaired"] = result["repaired"] or bool(result["temporaries_removed"])
    return result
