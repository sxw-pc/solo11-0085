"""Load synthetic scenarios from JSON, including safe delta inheritance."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = PROJECT_ROOT / "fixtures"
SCENARIO_DIR = FIXTURES_DIR / "scenarios"
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
REQUIRED_JOB_FIELDS = {
    "id", "title", "company", "location", "salary", "experience",
    "employment_type", "posted_at", "description", "tags",
}


@dataclass(frozen=True)
class ScenarioData:
    name: str
    description: str
    jobs: tuple[dict[str, Any], ...]
    listing: dict[str, Any]
    faults: dict[str, Any]
    omit_fields: dict[str, list[str]]

    def job(self, job_id: str) -> dict[str, Any] | None:
        for item in self.jobs:
            if item["id"] == job_id:
                return item
        return None


def scenario_names(fixtures_dir: Path = FIXTURES_DIR) -> tuple[str, ...]:
    directory = Path(fixtures_dir) / "scenarios"
    return tuple(sorted(path.stem for path in directory.glob("*.json")))


def _read(name: str, fixtures_dir: Path) -> dict[str, Any]:
    if not _NAME.match(name):
        raise ConfigError(f"invalid scenario name: {name!r}")
    path = Path(fixtures_dir) / "scenarios" / f"{name}.json"
    if not path.is_file():
        raise ConfigError(f"scenario not found: {name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read scenario {name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"scenario root must be an object: {name}")
    return payload


def _validate_jobs(jobs: list[Any], name: str) -> None:
    seen: set[str] = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ConfigError(f"{name}.jobs[{index}] must be an object")
        missing = REQUIRED_JOB_FIELDS - set(job)
        if missing:
            raise ConfigError(f"{name}.jobs[{index}] missing {sorted(missing)}")
        job_id = job["id"]
        if not isinstance(job_id, str) or not job_id.strip():
            raise ConfigError(f"{name}.jobs[{index}].id must be a string")
        if job_id in seen:
            raise ConfigError(f"{name}: duplicate job definition {job_id}")
        seen.add(job_id)
        if not isinstance(job["tags"], list):
            raise ConfigError(f"{name}.jobs[{index}].tags must be a list")


def _merge(raw: dict[str, Any], fixtures_dir: Path, depth: int = 0) -> dict[str, Any]:
    if depth > 5:
        raise ConfigError("scenario inheritance is too deep")
    parent = raw.get("extends")
    if parent:
        if not isinstance(parent, str):
            raise ConfigError("scenario extends must be a string")
        base = _merge(_read(parent, fixtures_dir), fixtures_dir, depth + 1)
    else:
        base = {key: copy.deepcopy(raw.get(key, {} if key != "jobs" else []))
                for key in ("jobs", "listing", "faults", "omit_fields")}
    jobs = [copy.deepcopy(job) for job in base["jobs"]]
    by_id = {job["id"]: job for job in jobs}
    for job_id in raw.get("remove_ids", []):
        if job_id not in by_id:
            raise ConfigError(f"remove_ids references unknown job: {job_id}")
        jobs = [job for job in jobs if job["id"] != job_id]
        by_id.pop(job_id, None)
    changes = raw.get("changes", {})
    if not isinstance(changes, dict):
        raise ConfigError("changes must be an object")
    for job_id, updates in changes.items():
        if job_id not in by_id:
            raise ConfigError(f"changes references unknown job: {job_id}")
        if not isinstance(updates, dict):
            raise ConfigError(f"changes for {job_id} must be an object")
        unknown = set(updates) - REQUIRED_JOB_FIELDS
        if unknown:
            raise ConfigError(f"unsupported fields for {job_id}: {sorted(unknown)}")
        by_id[job_id].update(copy.deepcopy(updates))
    additions = raw.get("add", [])
    if not isinstance(additions, list):
        raise ConfigError("add must be a list")
    jobs.extend(copy.deepcopy(additions))
    listing = copy.deepcopy(base.get("listing", {}))
    faults = copy.deepcopy(base.get("faults", {}))
    omit = copy.deepcopy(base.get("omit_fields", {}))
    for key, target in (("listing", listing), ("faults", faults), ("omit_fields", omit)):
        target.update(copy.deepcopy(raw.get(key, {})))
    _validate_jobs(jobs, str(raw.get("scenario", "scenario")))
    page_size = listing.get("page_size", 10)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ConfigError("listing.page_size must be an integer between 1 and 100")
    loop_to = listing.get("loop_last_to")
    if loop_to is not None and (
        isinstance(loop_to, bool) or not isinstance(loop_to, int) or loop_to < 1
    ):
        raise ConfigError("listing.loop_last_to must be a positive integer or null")
    for key in ("duplicate_ids", "malformed_card_ids", "missing_detail_ids"):
        value = listing.get(key, [])
        if not isinstance(value, list):
            raise ConfigError(f"listing.{key} must be a list")
        listing[key] = value
    if not isinstance(faults, dict):
        raise ConfigError("faults must be an object")
    return {"name": raw.get("scenario") or "", "description": str(raw.get("description", "")),
            "jobs": jobs, "listing": listing, "faults": faults, "omit_fields": omit}


def load_scenario(name: str, fixtures_dir: str | Path = FIXTURES_DIR) -> ScenarioData:
    directory = Path(fixtures_dir)
    raw = _read(name, directory)
    merged = _merge(raw, directory)
    declared = raw.get("scenario")
    if declared != name:
        raise ConfigError(f"scenario file {name}.json declares {declared!r}")
    job_ids = {job["id"] for job in merged["jobs"]}
    for key in ("duplicate_ids", "malformed_card_ids", "missing_detail_ids"):
        for job_id in merged["listing"].get(key, []):
            if job_id not in job_ids:
                raise ConfigError(f"listing.{key} references unknown job: {job_id}")
    omit = merged["omit_fields"]
    if not isinstance(omit, dict) or any(not isinstance(values, list) for values in omit.values()):
        raise ConfigError("omit_fields must map job ids to field lists")
    return ScenarioData(
        name=name, description=merged["description"], jobs=tuple(merged["jobs"]),
        listing=merged["listing"], faults=merged["faults"],
        omit_fields={key: list(value) for key, value in omit.items()},
    )
