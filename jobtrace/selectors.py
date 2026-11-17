"""CSS selector configuration for list and detail pages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .yamlio import read_yaml, strict_map, text

LIST_KEYS = {
    "item", "link", "job_id_attr", "title", "company", "location", "salary",
    "next_page", "prev_page", "page_state", "snapshot_id_attr", "page_index_attr",
    "total_pages_attr",
}
DETAIL_KEYS = {"title", "company", "location", "salary", "experience", "employment_type",
               "description", "posted_at", "status", "tags"}


@dataclass(frozen=True)
class ListSelectors:
    item: str
    link: str
    job_id_attr: str
    title: str
    company: str
    location: str
    salary: str
    next_page: str
    prev_page: str
    page_state: str
    snapshot_id_attr: str
    page_index_attr: str
    total_pages_attr: str


@dataclass(frozen=True)
class DetailSelectors:
    title: str
    company: str
    location: str
    salary: str
    experience: str
    employment_type: str
    description: str
    posted_at: str
    status: str
    tags: str


@dataclass(frozen=True)
class Selectors:
    list: ListSelectors
    detail: DetailSelectors


def parse_selectors(raw: Mapping[str, Any], source: str = "selectors") -> Selectors:
    top = strict_map(dict(raw), {"list", "detail"}, source)
    list_data = strict_map(top["list"], LIST_KEYS, f"{source}.list")
    detail_data = strict_map(top["detail"], DETAIL_KEYS, f"{source}.detail")
    list_sel = ListSelectors(
        **{key: text(list_data[key], f"{source}.list.{key}") for key in LIST_KEYS}
    )
    detail_sel = DetailSelectors(
        **{key: text(detail_data[key], f"{source}.detail.{key}") for key in DETAIL_KEYS}
    )
    if list_sel.item == "/":
        raise ConfigError(f"selectors file has an unsafe item selector: {source}")
    return Selectors(list=list_sel, detail=detail_sel)


def load_selectors(path: str | Path) -> Selectors:
    selector_path = Path(path)
    return parse_selectors(read_yaml(selector_path), str(selector_path))
