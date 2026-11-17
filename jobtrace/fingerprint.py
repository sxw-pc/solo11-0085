"""Stable identity and content fingerprints."""
from __future__ import annotations
import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping
TRACKED_FIELDS = ("title", "company", "location", "salary", "experience", "employment_type", "tags")
def canonical_json(value: Any) -> str:
    if is_dataclass(value): value = asdict(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
def sha256_text(text: str) -> str: return hashlib.sha256(text.encode("utf-8")).hexdigest()
def sha256_bytes(content: bytes) -> str: return hashlib.sha256(content).hexdigest()
def job_uid(site_key: str, external_id: str) -> str:
    """Identity is scoped by site so two sources may legally reuse an id."""
    return "job_" + sha256_text(f"{site_key}\x1f{external_id}")[:24]
def content_hash(payload: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(dict(payload)))
def config_fingerprint(config: Any, selectors: Any) -> str:
    payload = {
        "config": asdict(config) if is_dataclass(config) else config,
        "selectors": asdict(selectors) if is_dataclass(selectors) else selectors,
    }
    return sha256_text(canonical_json(payload))
