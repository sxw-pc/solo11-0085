"""Selector-driven parsing for listing and detail pages."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup, Tag
from .models import JobObservation, ListItem
from .normalize import (
    clean_text, normalize_employment_type, normalize_experience,
    normalize_location, normalize_tags,
)
from .salary import parse_salary
from .selectors import Selectors
from .requirements import parse_requirements
_JOB_ID = re.compile(r"/jobs/([^/?#]+)")
@dataclass(frozen=True)
class ListPage:
    items: tuple[ListItem, ...]
    next_url: str | None
    card_count: int
    issues: tuple[str, ...] = ()
    selector_misses: dict[str, int] = field(default_factory=dict)
@dataclass(frozen=True)
class DetailResult:
    observation: JobObservation | None
    required_missing: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    selector_misses: dict[str, int] = field(default_factory=dict)
    @property
    def ok(self) -> bool: return self.observation is not None
def _text(node: Tag | None) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node is not None else ""
def _select_one(root: Tag, selector: str) -> Tag | None:
    try:
        return root.select_one(selector)
    except Exception:  # BeautifulSoup raises several selector-specific errors.
        return None
def _select_all(root: Tag, selector: str) -> list[Tag]:
    try:
        return list(root.select(selector))
    except Exception:
        return []
def _missing(counter: dict[str, int], field_name: str) -> None:
    counter[field_name] = counter.get(field_name, 0) + 1
def parse_list_page(html: str, page_url: str, selectors: Selectors) -> ListPage:
    soup = BeautifulSoup(html, "html.parser")
    cards = _select_all(soup, selectors.list.item)
    misses: dict[str, int] = {}
    issues: list[str] = []
    items: list[ListItem] = []
    for index, card in enumerate(cards, start=1):
        link = _select_one(card, selectors.list.link)
        href = (link.get("href") or "").strip() if link is not None else ""
        if not href:
            issues.append(f"card[{index}]: missing detail link")
            _missing(misses, "link")
            continue
        external_id = clean_text(card.get(selectors.list.job_id_attr) or "")
        if not external_id:
            match = _JOB_ID.search(urlsplit(urljoin(page_url, href)).path)
            external_id = match.group(1) if match else ""
        if not external_id:
            issues.append(f"card[{index}]: missing job id")
            _missing(misses, "job_id")
            continue
        title = _text(_select_one(card, selectors.list.title))
        company = _text(_select_one(card, selectors.list.company))
        location = _text(_select_one(card, selectors.list.location))
        salary = _text(_select_one(card, selectors.list.salary))
        card_issues: list[str] = []
        for name, value, key in (
            ("title", title, "title"), ("company", company, "company"),
            ("location", location, "location"), ("salary", salary, "salary"),
        ):
            if not value:
                _missing(misses, key)
                card_issues.append(f"card[{index}]: missing {name}")
        issues.extend(card_issues)
        items.append(ListItem(
            external_id=external_id, title=title, company=company, location=location,
            salary=salary, detail_url=urljoin(page_url, href), issues=tuple(card_issues),
        ))
    next_node = _select_one(soup, selectors.list.next_page)
    next_href = (next_node.get("href") or "").strip() if next_node is not None else ""
    if not next_node: _missing(misses, "next_page")
    return ListPage(
        items=tuple(items), next_url=urljoin(page_url, next_href) if next_href else None,
        card_count=len(cards), issues=tuple(issues), selector_misses=misses,
    )
def _optional(soup: BeautifulSoup, selector: str, key: str,
              misses: dict[str, int], issues: list[str]) -> str:
    value = _text(_select_one(soup, selector))
    if not value:
        _missing(misses, key)
        issues.append(f"missing optional field: {key}")
    return value
def parse_detail_page(html: str, page_url: str, site_key: str,
                      item: ListItem, selectors: Selectors) -> DetailResult:
    soup = BeautifulSoup(html, "html.parser")
    misses: dict[str, int] = {}
    issues: list[str] = []
    title = _optional(soup, selectors.detail.title, "title", misses, issues)
    company = _optional(soup, selectors.detail.company, "company", misses, issues)
    required_missing = tuple(
        name for name, value in (("title", title), ("company", company)) if not value
    )
    if required_missing:
        issues.append("missing required field(s): " + ", ".join(required_missing))
        return DetailResult(observation=None, required_missing=required_missing,
                            issues=tuple(issues), selector_misses=misses)
    location_raw = _optional(soup, selectors.detail.location, "location", misses, issues)
    salary_raw = _optional(soup, selectors.detail.salary, "salary", misses, issues)
    experience_raw = _optional(soup, selectors.detail.experience, "experience", misses, issues)
    employment_raw = _optional(
        soup, selectors.detail.employment_type, "employment_type", misses, issues
    )
    description = _optional(soup, selectors.detail.description, "description", misses, issues)
    posted_at = _optional(soup, selectors.detail.posted_at, "posted_at", misses, issues)
    status = _optional(soup, selectors.detail.status, "status", misses, issues)
    tag_nodes = _select_all(soup, selectors.detail.tags)
    if not tag_nodes:
        _missing(misses, "tags")
        issues.append("missing optional field: tags")
    salary = parse_salary(salary_raw)
    if salary_raw and not salary.valid:
        issues.append(f"invalid salary expression: {salary_raw}")
    location = normalize_location(location_raw)
    observation = JobObservation(
        site_key=site_key, external_id=item.external_id, title=title, company=company,
        location_raw=location.raw, city=location.city, district=location.district,
        salary=salary, experience=normalize_experience(experience_raw),
        employment_type=normalize_employment_type(employment_raw),
        description=description,
        tags=normalize_tags([node.get_text(" ", strip=True) for node in tag_nodes]),
        detail_url=page_url, posted_at=posted_at or None, source_status=status or None,
        issues=tuple(issues), requirement_parse=parse_requirements(description),
    )
    return DetailResult(observation=observation, issues=tuple(issues), selector_misses=misses)
def merge_selector_misses(*maps: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for mapping in maps:
        for key, value in mapping.items(): merged[key] = merged.get(key, 0) + value
    return dict(sorted(merged.items()))
