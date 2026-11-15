"""Text, location, experience, and tag normalization."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    raw: str
    city: str | None
    district: str | None


@dataclass(frozen=True)
class Experience:
    text: str | None
    min_years: int | None
    max_years: int | None


_SEPARATORS = re.compile(r"\s*[·•/|,，;；—–-]\s*")
_EXPERIENCE_RANGE = re.compile(r"(?P<low>\d{1,2})(?:\s*[-~至]\s*)(?P<high>\d{1,2})\s*年")
_EXPERIENCE_PLUS = re.compile(r"(?P<low>\d{1,2})\s*年(?:以上|及以上|\+)")
_EXPERIENCE_BELOW = re.compile(r"(?P<high>\d{1,2})\s*年(?:以下|以内)")
_EXPERIENCE_SINGLE = re.compile(r"(?P<years>\d{1,2})\s*年")


def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\xa0", " ").replace("　", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_location(value: str | None) -> Location:
    raw = clean_text(value)
    if not raw:
        return Location(raw="", city=None, district=None)
    parts = [part.strip() for part in _SEPARATORS.split(raw) if part.strip()]
    if not parts:
        return Location(raw=raw, city=None, district=None)
    city = parts[0]
    if city.endswith("市") and len(city) > 2:
        city = city[:-1]
    district = " ".join(parts[1:]) if len(parts) > 1 else None
    if city in {"远程", "Remote", "remote"}:
        return Location(raw=raw, city="远程", district=district)
    return Location(raw=raw, city=city, district=district)


def normalize_experience(value: str | None) -> Experience:
    raw = clean_text(value)
    if not raw:
        return Experience(text=None, min_years=None, max_years=None)
    if any(token in raw for token in ("不限", "无经验", "无需经验")):
        return Experience(text="不限", min_years=None, max_years=None)
    if "应届" in raw:
        return Experience(text="应届", min_years=0, max_years=1)
    match = _EXPERIENCE_RANGE.search(raw)
    if match:
        low, high = int(match.group("low")), int(match.group("high"))
        if low > high:
            low, high = high, low
        return Experience(text=f"{low}-{high}年", min_years=low, max_years=high)
    match = _EXPERIENCE_PLUS.search(raw)
    if match:
        low = int(match.group("low"))
        return Experience(text=f"{low}年以上", min_years=low, max_years=None)
    match = _EXPERIENCE_BELOW.search(raw)
    if match:
        high = int(match.group("high"))
        return Experience(text=f"{high}年以内", min_years=0, max_years=high)
    match = _EXPERIENCE_SINGLE.search(raw)
    if match:
        years = int(match.group("years"))
        return Experience(text=f"{years}年", min_years=years, max_years=years)
    return Experience(text=raw, min_years=None, max_years=None)


def normalize_employment_type(value: str | None) -> str | None:
    raw = clean_text(value)
    if not raw:
        return None
    mapping = {"全职": "全职", "全日制": "全职", "兼职": "兼职", "实习": "实习",
               "实习生": "实习", "合同": "合同", "合同制": "合同", "外包": "外包",
               "临时": "临时"}
    for token, label in mapping.items():
        if token in raw:
            return label
    return raw


def normalize_tags(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    tags: list[str] = []
    for value in values or ():
        tag = clean_text(value)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tuple(tags)
