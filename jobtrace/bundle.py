"""Report bundle storage: layout, atomic writes, verification, and readers.

Layout under a report directory::

    bundles/<bundle_id>/report.json|report.md|jobs.csv|events.jsonl
    bundles/<bundle_id>/manifest.json
    published.json                 derived pointer journal, replaced atomically
    preview/<bundle_id>.json       candidate index for one generation attempt

The single atomic commit point is the ``current`` symlink: one rename switches
all four stable paths together.  Preview readers never touch ``current``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .fingerprint import sha256_bytes

FORMATS = ("json", "markdown", "csv", "jsonl")
FILENAMES = {
    "json": "report.json", "markdown": "report.md",
    "csv": "jobs.csv", "jsonl": "events.jsonl",
}
PUBLISHED_POINTER = "published.json"
BUNDLES_DIR = "bundles"
PREVIEW_DIR = "preview"
CURRENT_LINK = "current"
MANIFEST = "manifest.json"


class ReportBundleError(Exception):
    """Raised when a candidate bundle cannot be verified or published."""


class Writes:
    """``Path.write_text`` indirection so the exact failing write point is testable."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def text(self, path: Path, content: str, newline: str | None = None) -> None:
        self.calls.append(Path(path))
        Path(path).write_text(content, encoding="utf-8", newline=newline)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None

def digest_text(content: str) -> str:
    return "sha256:" + sha256_bytes(content.encode("utf-8"))


def digest_file(path: Path) -> str:
    return "sha256:" + sha256_bytes(path.read_bytes())


def bundle_dir(output: Path, bundle_id: str) -> Path:
    return output / BUNDLES_DIR / bundle_id


def manifest_formats(output: Path, bundle_id: str) -> dict[str, str]:
    manifest = read_json(bundle_dir(output, bundle_id) / MANIFEST)
    formats = (manifest or {}).get("formats")
    return dict(formats) if isinstance(formats, dict) else {}


def verified_formats(folder: Path, expected: dict[str, str]) -> tuple[list[str], list[str]]:
    done: list[str] = []
    missing: list[str] = []
    for name in FORMATS:
        target = folder / FILENAMES[name]
        if name not in expected or not target.is_file():
            missing.append(name)
            continue
        try:
            digest = digest_file(target)
        except OSError:
            missing.append(name)
            continue
        (done if digest == expected[name] else missing).append(name)
    return done, missing


def dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_manifest(folder: Path, report: dict[str, Any], formats: dict[str, str],
                   completed: list[str] | None = None, published: bool = False) -> None:
    bundle = report["bundle"]
    write_atomic(folder / MANIFEST, dump({
        "bundle_id": bundle["bundle_id"], "run_id": bundle["run_id"],
        "content_digest": bundle["content_digest"], "formats": formats,
        "observation_scope": bundle.get("observation_scope"),
        "completed_formats": completed if completed is not None
        else [name for name in FORMATS if name in formats],
        "published": published,
    }))


def verify_bundle(folder: Path, bundle_id: str) -> dict[str, Any]:
    """Re-read a bundle directory; never trust a manifest alone."""

    manifest = read_json(folder / MANIFEST)
    if not manifest or manifest.get("bundle_id") != bundle_id:
        raise ReportBundleError(f"bundle manifest missing or foreign: {bundle_id}")
    expected = manifest.get("formats") or {}
    missing = verified_formats(folder, expected)[1]
    if missing:
        raise ReportBundleError(
            f"bundle incomplete: {bundle_id} missing={missing}")
    json_path = folder / FILENAMES["json"]
    json_bytes = json_path.read_bytes()
    parsed = json.loads(json_bytes.decode("utf-8"))
    bundle = parsed.get("bundle") or {}
    if bundle.get("bundle_id") != bundle_id:
        raise ReportBundleError(f"json bundle identity mismatch: {bundle_id}")
    if "sha256:" + sha256_bytes(json_bytes) != expected["json"]:
        raise ReportBundleError(f"json digest mismatch: {bundle_id}")
    for name in ("markdown", "csv", "jsonl"):
        content = (folder / FILENAMES[name]).read_text(encoding="utf-8")
        if bundle["bundle_id"] not in content or bundle["content_digest"] not in content:
            raise ReportBundleError(f"{name} lacks batch identity: {bundle_id}")
    return manifest


def published_pointer(output: Path) -> dict[str, Any] | None:
    pointer = read_json(output / PUBLISHED_POINTER)
    return pointer if pointer and isinstance(pointer.get("bundle_id"), str) else None


def current_bundle(output: Path) -> str | None:
    """The atomic commit point: one symlink rename switches all four formats."""

    try:
        target = os.readlink(output / CURRENT_LINK)
    except OSError:
        return None
    name = Path(target).name
    return name if Path(target).parent.name == BUNDLES_DIR else None


def published_state(output: Path) -> dict[str, Any] | None:
    if (bundle_id := current_bundle(output)) is None:
        return None
    pointer = published_pointer(output)
    try:
        manifest = verify_bundle(bundle_dir(output, bundle_id), bundle_id)
    except (ReportBundleError, OSError, ValueError):
        return {"bundle_id": bundle_id, "state": "corrupt", "manifest": None,
                "pointer": pointer}
    state = "published"
    pointer_ok = bool(pointer) and pointer.get("bundle_id") == bundle_id and (
        manifest.get("content_digest") == pointer.get("content_digest"))
    if not pointer_ok:
        state = "needs_recovery"
    return {"bundle_id": bundle_id, "state": state, "manifest": manifest,
            "pointer": pointer}



def read_published(output_dir: str | Path) -> dict[str, Any]:
    """Published contract: one all-or-nothing, verified four-format bundle."""

    output = Path(output_dir)
    state = published_state(output)
    if state is None:
        return {"status": "empty", "bundle_id": None, "formats": {}, "paths": {},
                "completed_formats": [], "missing_formats": list(FORMATS),
                "reason": "no published pointer"}
    bundle_id, manifest = state["bundle_id"], state["manifest"]
    folder = bundle_dir(output, bundle_id)
    paths = {name: str((output / FILENAMES[name]).absolute()) for name in FORMATS}
    resolved = {name: str((folder / FILENAMES[name]).resolve()) for name in FORMATS}
    if state["state"] == "corrupt" or any(
            digest_file(folder / FILENAMES[name]) != manifest["formats"][name]
            for name in FORMATS):
        return {"status": "corrupt", "bundle_id": bundle_id, "formats": {}, "paths": paths,
                "completed_formats": [], "missing_formats": list(FORMATS),
                "reason": "committed bundle failed verification"}
    pointer = state["pointer"] or {}
    return {
        "status": "published", "bundle_id": bundle_id,
        "run_id": pointer.get("run_id") or manifest.get("run_id"),
        "content_digest": manifest.get("content_digest"),
        "formats": resolved, "paths": paths,
        "completed_formats": list(FORMATS), "missing_formats": [],
        "verified_digests": manifest.get("formats", {}),
        "pointer_status": state["state"],
        "reason": ("verified atomic bundle" if state["state"] == "published"
                   else "verified bundle; published pointer needs recovery"),
    }


def read_preview(output_dir: str | Path, bundle_id: str | None = None) -> dict[str, Any]:
    """Preview contract: candidate batch ID plus completed/missing formats."""

    output = Path(output_dir)
    index = output / PREVIEW_DIR
    chosen = None
    if bundle_id is not None:
        chosen = read_json(index / f"{bundle_id}.json")
    elif index.is_dir():
        for candidate in sorted(index.glob("*.json"),
                                key=lambda p: (p.stat().st_mtime_ns, p.name),
                                reverse=True):
            chosen = read_json(candidate)
            if chosen and chosen.get("bundle_id"):
                break
    if not chosen or not chosen.get("bundle_id"):
        return {"status": "empty", "bundle_id": None, "run_id": None,
                "completed_formats": [], "missing_formats": list(FORMATS),
                "formats": {}, "reason": "no preview candidate"}
    identity = chosen["bundle_id"]
    folder = output / chosen.get("bundle_dir", f"{BUNDLES_DIR}/{identity}")
    expected = chosen.get("formats") or {}
    done, missing = verified_formats(folder, expected)
    state = "candidate_complete" if not missing else (
        "candidate_partial" if done else "empty")
    return {
        "status": state, "bundle_id": identity, "run_id": chosen.get("run_id"),
        "completed_formats": done, "missing_formats": missing,
        "formats": {name: str((folder / FILENAMES[name]).resolve()) for name in done},
        "reason": chosen.get("reason", ""),
    }


def write_preview(output: Path, report: dict[str, Any], status: str,
                  reason: str = "") -> Path:
    bundle = report["bundle"]
    bundle_id = bundle["bundle_id"]
    target = output / PREVIEW_DIR / f"{bundle_id}.json"
    write_atomic(target, dump({
        "bundle_id": bundle_id, "run_id": bundle.get("run_id"),
        "content_digest": bundle["content_digest"], "status": status,
        "reason": reason, "bundle_dir": f"{BUNDLES_DIR}/{bundle_id}",
        "formats": manifest_formats(output, bundle_id),
    }))
    return target


def bundle_paths(output: Path, bundle_id: str) -> dict[str, str]:
    return {name: str((bundle_dir(output, bundle_id) / FILENAMES[name]).resolve())
            for name in FORMATS}


def link_current(output: Path, bundle_id: str) -> None:
    """Atomically repoint ``current`` and the four stable format paths at it."""

    _replace_with_link(output / CURRENT_LINK, f"{BUNDLES_DIR}/{bundle_id}", "tmp")
    for name in FORMATS:
        legacy = output / FILENAMES[name]
        wanted = f"{CURRENT_LINK}/{FILENAMES[name]}"
        if not (legacy.is_symlink() and os.readlink(legacy) == wanted):
            _replace_with_link(legacy, wanted, "link")


def _replace_with_link(path: Path, target: str, tag: str) -> None:
    temporary = path.with_name(f".{path.name}.{tag}-{os.getpid()}")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, path)


def remove_temporaries(output_dir: str | Path) -> list[str]:
    """Remove leftover temp files from an interrupted manifest/pointer swap."""

    output = Path(output_dir)
    stale = {p for pattern in (".*.tmp-*", ".*.link-*", "**/.*.tmp-*")
             for p in output.glob(pattern) if p.is_file()}
    for path in stale:
        path.unlink()
    return sorted(map(str, stale))


Render = Callable[[dict[str, Any]], dict[str, str]]
