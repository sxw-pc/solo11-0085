"""Shared domain value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fingerprint import content_hash
from .normalize import Experience
from .requirements import (
    DEFAULT_RULES,
    RequirementParse,
    audit_revision,
    parse_requirements,
)
from .salary import SalaryInfo


@dataclass(frozen=True)
class ListItem:
    external_id: str
    title: str
    company: str
    location: str
    salary: str
    detail_url: str
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobObservation:
    site_key: str
    external_id: str
    title: str
    company: str
    location_raw: str
    city: str | None
    district: str | None
    salary: SalaryInfo
    experience: Experience
    employment_type: str | None
    description: str
    tags: tuple[str, ...]
    detail_url: str
    posted_at: str | None = None
    source_status: str | None = None
    issues: tuple[str, ...] = field(default_factory=tuple)
    requirement: RequirementParse | None = None

    def content_payload(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id, "title": self.title, "company": self.company,
            "location_raw": self.location_raw, "city": self.city, "district": self.district,
            "salary_min": self.salary.min_monthly, "salary_max": self.salary.max_monthly,
            "salary_text": self.salary.normalized, "salary_months": self.salary.months_per_year,
            "salary_period": self.salary.source_period,
            "salary_negotiable": self.salary.negotiable,
            "experience_text": self.experience.text,
            "experience_min": self.experience.min_years,
            "experience_max": self.experience.max_years,
            "employment_type": self.employment_type, "description": self.description,
            "tags": list(self.tags), "detail_url": self.detail_url,
            "posted_at": self.posted_at, "source_status": self.source_status,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.content_payload())

    def requirement_parse(self, rules=DEFAULT_RULES) -> RequirementParse:
        return self.requirement or parse_requirements(self.description, rules)

    def requirement_audit(self, old_description: str | None,
                          rules=DEFAULT_RULES) -> dict[str, Any]:
        return audit_revision(old_description, self.description, rules)

    def tracked_values(self) -> dict[str, str]:
        tags = "、".join(self.tags)
        return {"title": self.title, "company": self.company,
                "location": self.location_raw, "salary": self.salary.normalized,
                "experience": self.experience.text or "",
                "employment_type": self.employment_type or "", "tags": tags}
