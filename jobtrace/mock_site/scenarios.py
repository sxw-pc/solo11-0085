"""Scenario switching and deterministic fault injection for the mock site."""
from __future__ import annotations
import copy
import math
import threading
from pathlib import Path
from typing import Any
from ..errors import ConfigError
from .data import FIXTURES_DIR, ScenarioData, load_scenario, scenario_names
class FaultInjector:
    """Each configured key fails exactly N times, then succeeds."""
    def __init__(self, faults: dict[str, Any]):
        self._lock = threading.Lock()
        self._faults = copy.deepcopy(faults)
    def _counts(self, kind: str) -> dict[str, int]:
        values = self._faults.get(kind, {})
        if not isinstance(values, dict): raise ConfigError(f"faults.{kind} must be an object")
        counts: dict[str, int] = {}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"faults.{kind}.{key} must be a non-negative integer")
            counts[str(key)] = value
        return counts
    def consume(self, kind: str, key: str) -> bool:
        with self._lock:
            counts = self._counts(kind)
            remaining = counts.get(str(key), 0)
            if remaining <= 0: return False
            counts[str(key)] = remaining - 1
            self._faults[kind] = counts
            return True
    def remaining(self, kind: str, key: str) -> int:
        with self._lock: return self._counts(kind).get(str(key), 0)
class ScenarioController:
    def __init__(self, scenario: str = "baseline", *,
                 fixtures_dir: str | Path = FIXTURES_DIR, page_size: int | None = None):
        self.fixtures_dir = Path(fixtures_dir)
        self._page_override = page_size
        self._lock = threading.Lock()
        self.name = ""
        self.data: ScenarioData
        self.injector: FaultInjector
        self.switch(scenario)
    def switch(self, name: str) -> None:
        data = load_scenario(name, self.fixtures_dir)
        with self._lock:
            self.name = name
            self.data = data
            self.injector = FaultInjector(data.faults)
    @property
    def page_size(self) -> int:
        if self._page_override: return self._page_override
        return int(self.data.listing.get("page_size", 10))
    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.data.jobs) / self.page_size))
    def _duplicate_on_page(self, job_id: str, page: int) -> dict[str, Any] | None:
        for index, job in enumerate(self.data.jobs):
            if job["id"] == job_id:
                if index // self.page_size + 1 == page: return copy.deepcopy(job)
                return None
        return None
    def page_cards(self, page: int) -> tuple[list[dict[str, Any]], str | None]:
        if not 1 <= page <= self.total_pages: raise IndexError("page out of range")
        start = (page - 1) * self.page_size
        cards = [copy.deepcopy(job) for job in self.data.jobs[start : start + self.page_size]]
        malformed = set(self.data.listing.get("malformed_card_ids", []))
        for card in cards: card["malformed"] = card["id"] in malformed
        for job_id in self.data.listing.get("duplicate_ids", []):
            duplicate = self._duplicate_on_page(str(job_id), page)
            if duplicate is not None:
                duplicate["malformed"] = duplicate["id"] in malformed
                duplicate["duplicate_card"] = True
                cards.append(duplicate)
        if page < self.total_pages:
            next_url = f"/jobs?page={page + 1}"
        else:
            loop_to = self.data.listing.get("loop_last_to")
            next_url = f"/jobs?page={loop_to}" if loop_to and self.total_pages > 1 else None
        return cards, next_url
    def list_fault(self, page: int) -> bool:
        return self.injector.consume("list_503", str(page))
    def detail_fault(self, job_id: str) -> bool:
        return self.injector.consume("detail_503", str(job_id))
    def is_missing_detail(self, job_id: str) -> bool:
        return job_id in set(self.data.listing.get("missing_detail_ids", []))
    def omitted_fields(self, job_id: str) -> set[str]:
        return set(self.data.omit_fields.get(job_id, []))
    def job(self, job_id: str) -> dict[str, Any] | None: return self.data.job(job_id)
    def info(self) -> dict[str, Any]:
        return {"scenario": self.name, "description": self.data.description,
                "jobs": len(self.data.jobs), "page_size": self.page_size,
                "pages": self.total_pages, "available": list(scenario_names(self.fixtures_dir))}
