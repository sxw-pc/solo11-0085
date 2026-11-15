"""Pagination guards and resumable list-page cursors."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import PaginationError, PaginationLimitError, PaginationLoopError


def normalized_page_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))


@dataclass
class Cursor:
    version: int
    site_key: str
    next_url: str | None
    visited_pages: list[str]
    page_count: int
    completed: bool
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)

    @classmethod
    def load(cls, path: str | Path) -> Cursor | None:
        source = Path(path)
        if not source.is_file():
            return None
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            cursor = cls(
                version=int(payload["version"]), site_key=str(payload["site_key"]),
                next_url=payload.get("next_url"),
                visited_pages=[str(url) for url in payload["visited_pages"]],
                page_count=int(payload["page_count"]), completed=bool(payload["completed"]),
                updated_at=str(payload["updated_at"]),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PaginationError(f"invalid pagination cursor: {source}: {exc}") from exc
        if cursor.version != 1:
            raise PaginationError(f"unsupported pagination cursor version: {cursor.version}")
        return cursor


@dataclass
class PaginationGuard:
    max_pages: int
    visited: list[str] = field(default_factory=list)
    reason: str | None = None

    def start(self, url: str, visited: list[str] | None = None) -> None:
        self.visited = [normalized_page_url(item) for item in (visited or [])]
        current = normalized_page_url(url)
        if current not in self.visited:
            self.visited.append(current)
        if len(self.visited) > self.max_pages:
            raise PaginationLimitError(
                f"cursor already contains {len(self.visited)} pages; max is {self.max_pages}"
            )

    @property
    def page_count(self) -> int:
        return len(self.visited)

    def advance(self, next_url: str | None) -> str | None:
        if not next_url:
            self.reason = "natural_end"
            return None
        if self.page_count >= self.max_pages:
            self.reason = "max_pages"
            raise PaginationLimitError(f"page limit reached: {self.max_pages}")
        normalized = normalized_page_url(next_url)
        if normalized in self.visited:
            self.reason = "loop"
            raise PaginationLoopError(f"repeated pagination URL: {normalized}")
        self.visited.append(normalized)
        return next_url

    def cursor(self, *, site_key: str, next_url: str | None,
               updated_at: str, completed: bool) -> Cursor:
        return Cursor(version=1, site_key=site_key, next_url=next_url,
                      visited_pages=list(self.visited), page_count=self.page_count,
                      completed=completed, updated_at=updated_at)
