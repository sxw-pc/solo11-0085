"""Controlled synthetic employer registry: dated aliases, mutex relations, groups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import ConfigError
from .normalize import clean_text
from .yamlio import read_yaml, text


@dataclass(frozen=True)
class Employer:
    employer_id: str
    legal_name: str
    group_id: str | None = None


@dataclass(frozen=True)
class AliasRule:
    display: str
    employer_id: str
    valid_from: date
    valid_to: date

    def __post_init__(self) -> None:
        # Display names live in the same normalized space as observed text.
        object.__setattr__(self, "display", clean_text(self.display))

    def covers(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass(frozen=True)
class EmployerRegistry:
    """Versioned local mapping from display names to legal employers."""

    version: str
    employers: dict[str, Employer]
    groups: dict[str, str]
    aliases: tuple[AliasRule, ...]
    mutex: tuple[tuple[str, str], ...] = ()

    def candidates(self, display: str, day: date) -> tuple[str, ...]:
        """Sorted legal employer ids the display name may denote on `day`."""
        display = clean_text(display)
        return tuple(sorted({
            rule.employer_id for rule in self.aliases
            if rule.display == display and rule.covers(day)
        }))

    def group_of(self, employer_id: str) -> str:
        """Management group id, falling back to the employer itself."""
        employer = self.employers[employer_id]
        return employer.group_id or employer.employer_id


def _parse_date(value: object, context: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{context} must be an ISO date string")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{context} must be ISO YYYY-MM-DD, got {value!r}") from exc


def _rows(data: dict, key: str, required: bool = True) -> list:
    value = data.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value):
        raise ConfigError(f"employer registry: {key} must be a non-empty list")
    return value


def _mapping(row: object, context: str) -> dict:
    if not isinstance(row, dict):
        raise ConfigError(f"employer registry: {context} entries must be mappings")
    return row


def load_registry(path: str | Path) -> EmployerRegistry:
    """Load and strictly validate a versioned synthetic employer registry."""
    data = read_yaml(Path(path))
    unknown = set(data) - {"registry_version", "groups", "employers", "aliases", "mutex"}
    if unknown:
        raise ConfigError(f"employer registry has unknown keys: {sorted(unknown)}")
    version = text(data.get("registry_version"), "registry_version")
    groups: dict[str, str] = {}
    for row in _rows(data, "groups", required=False):
        row = _mapping(row, "groups[]")
        group_id, name = text(row.get("id"), "groups[].id"), text(row.get("name"), "groups[].name")
        if group_id in groups:
            raise ConfigError(f"duplicate group id: {group_id}")
        groups[group_id] = name
    employers: dict[str, Employer] = {}
    for row in _rows(data, "employers"):
        row = _mapping(row, "employers[]")
        employer_id = text(row.get("id"), "employers[].id")
        legal_name = text(row.get("legal_name"), "employers[].legal_name")
        group_id = row.get("group")
        if group_id is not None:
            group_id = text(group_id, "employers[].group")
            if group_id not in groups:
                raise ConfigError(f"employer {employer_id} references unknown group {group_id}")
        if employer_id in employers:
            raise ConfigError(f"duplicate employer id: {employer_id}")
        employers[employer_id] = Employer(employer_id, legal_name, group_id)
    aliases: list[AliasRule] = []
    for row in _rows(data, "aliases"):
        row = _mapping(row, "aliases[]")
        display = clean_text(text(row.get("display"), "aliases[].display"))
        employer_id = text(row.get("employer"), "aliases[].employer")
        if employer_id not in employers:
            raise ConfigError(f"alias {display} references unknown employer {employer_id}")
        start = _parse_date(row.get("valid_from"), "aliases[].valid_from")
        end = _parse_date(row.get("valid_to"), "aliases[].valid_to")
        if start > end:
            raise ConfigError(f"alias {display}: valid_from is after valid_to")
        for existing in aliases:
            same = (existing.display, existing.employer_id) == (display, employer_id)
            if same and not (existing.valid_to < start or end < existing.valid_from):
                raise ConfigError(f"conflicting alias rules for {display} -> {employer_id}")
        aliases.append(AliasRule(display, employer_id, start, end))
    displays = {rule.display for rule in aliases}
    mutex: list[tuple[str, str]] = []
    for row in _rows(data, "mutex", required=False):
        if not isinstance(row, list) or len(row) != 2:
            raise ConfigError("mutex entries must be pairs of display names")
        pair = tuple(sorted(clean_text(text(item, "mutex[]")) for item in row))
        if pair[0] == pair[1]:
            raise ConfigError("mutex entries must name two distinct displays")
        missing = [name for name in pair if name not in displays]
        if missing:
            raise ConfigError(f"mutex references unknown displays: {missing}")
        if pair not in mutex:
            mutex.append(pair)
    return EmployerRegistry(version, employers, groups, tuple(aliases), tuple(mutex))
