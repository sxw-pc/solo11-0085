"""技能经验资格查询的数据模型：完全离线、无个人信息，仅消费合成履历与岗位要求。

合同（调用方必须显式选择，对应招聘方的两种口径）：
- ``exposure``（接触经验）：同一个月可为多项技能各记一次接触，只要当月确有该技能投入；
- ``dedicated``（专属投入）：每月可计容量上限 1 FTE，须在技能之间分配，
  判定所有技能下限是否存在一组全局可行分配（最大流），不按子句顺序贪心。

履历处置规则（均写入审计 issues）：半开区间 [start, end)；跨月按覆盖天数/当月天数折算；
同月多段并存但当月可计总投入上限为 1；完全重复的段落去重；起止倒置、空技能、比例不在
(0, 1] 的段落剔除。技能没有任何可核实投入时返回“证据不足”，不断言零年经验。
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Iterable, Iterator

from .normalize import clean_text

CONTRACTS = ("exposure", "dedicated")
SATISFIED, UNMET, NO_EVIDENCE = "satisfied", "unmet", "insufficient_evidence"
QUALIFIED, UNQUALIFIED = "qualified", "unqualified"


class RequirementError(ValueError):
    """岗位要求超出受限语法：仅接受 AND/且 连接的“技能 >= 正整月数”子句。"""


@dataclass(frozen=True)
class SkillClause:
    key: str
    label: str
    min_months: int


@dataclass(frozen=True)
class SkillRequirement:
    clauses: tuple[SkillClause, ...]
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "clauses": [
            {"skill": clause.label, "min_months": clause.min_months} for clause in self.clauses]}


_CLAUSE = re.compile(
    r"(?P<skill>[\w+#.\-]+)\s*(?:>=|≥|至少)\s*(?P<months>\d{1,3})\s*(?:个?月|months?)?")
_AND = re.compile(r"\s*(?:\bAND\b|且)\s*", re.IGNORECASE)


def skill_key(value: str) -> str:
    return clean_text(value).casefold()


def parse_requirement(text: str) -> SkillRequirement:
    raw = clean_text(text)
    if not raw:
        raise RequirementError("要求表达式为空")
    clauses: list[SkillClause] = []
    for part in _AND.split(raw):
        match = _CLAUSE.fullmatch(part.strip())
        if not match or int(match.group("months")) < 1:
            raise RequirementError(
                f"无法解析子句 {part!r}；仅接受 AND/且 连接的“技能 >= 正整月数”，"
                "“3-5 年”“应届”“不限”等仍由 normalize_experience 归一，不作多技能解释")
        key = skill_key(match.group("skill"))
        if any(clause.key == key for clause in clauses):
            raise RequirementError(f"技能 {match.group('skill')!r} 的子句重复")
        clauses.append(SkillClause(key, match.group("skill"), int(match.group("months"))))
    return SkillRequirement(tuple(clauses), source=raw)


def requirement_from_dict(payload: dict[str, Any]) -> SkillRequirement:
    clauses = tuple(SkillClause(skill_key(str(item["skill"])), clean_text(str(item["skill"])),
                                int(item["min_months"])) for item in payload.get("clauses", ()))
    if not clauses or any(not c.key or c.min_months < 1 for c in clauses):
        raise RequirementError("要求字典缺少有效子句")
    if len({c.key for c in clauses}) != len(clauses):
        raise RequirementError("要求字典中技能子句重复")
    return SkillRequirement(clauses, source=str(payload.get("source", "")))


@dataclass(frozen=True)
class ResumeEntry:
    """一段合成履历：半开区间 [start, end)，技能集合，每月投入比例 (0, 1]。"""

    start: date
    end: date
    skills: tuple[str, ...]
    ratio: Fraction = Fraction(1)
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "skills": list(self.skills), "ratio": str(self.ratio), "label": self.label}


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _as_ratio(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    try:
        return Fraction(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"投入比例无法解析: {value!r}") from exc


def make_entry(start: Any, end: Any, skills: Iterable[str], ratio: Any = 1,
               label: str = "") -> ResumeEntry:
    return ResumeEntry(_as_date(start), _as_date(end), tuple(skills), _as_ratio(ratio), str(label))


def entry_from_dict(payload: dict[str, Any]) -> ResumeEntry:
    return make_entry(payload["start"], payload["end"], payload.get("skills", ()),
                      payload.get("ratio", 1), payload.get("label", ""))


@dataclass(frozen=True)
class Segment:
    seg_id: str
    label: str
    start: date
    end: date
    skills: tuple[str, ...]
    ratio: Fraction


def validate_entries(entries: list[ResumeEntry]) -> tuple[list[Segment], list[dict[str, Any]]]:
    segments: list[Segment] = []
    issues: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], str] = {}
    for index, entry in enumerate(entries, 1):
        tag = entry.label or f"第{index}段"
        if entry.end <= entry.start:
            issues.append({"code": "inverted_dates", "segment": tag,
                           "detail": f"{tag} 起止倒置或为空（{entry.start}..{entry.end}），已剔除"})
            continue
        skills = tuple(dict.fromkeys(k for k in (skill_key(s) for s in entry.skills) if k))
        if not skills:
            issues.append({"code": "empty_skills", "segment": tag,
                           "detail": f"{tag} 未登记任何技能，已剔除"})
            continue
        if not Fraction(0) < entry.ratio <= Fraction(1):
            issues.append({"code": "invalid_ratio", "segment": tag,
                           "detail": f"{tag} 投入比例 {entry.ratio} 不在 (0, 1]，已剔除"})
            continue
        signature = (entry.start, entry.end, skills, entry.ratio)
        if signature in seen:
            issues.append({"code": "duplicate_entry", "segment": tag,
                           "detail": f"{tag} 与 {seen[signature]} 完全重复，已去重"})
            continue
        seg_id = f"seg-{len(segments) + 1}"
        seen[signature] = seg_id
        segments.append(Segment(seg_id, tag, entry.start, entry.end, skills, entry.ratio))
    return segments, issues


def segment_months(seg: Segment) -> Iterator[tuple[str, Fraction]]:
    last = seg.end - timedelta(days=1)
    year, month = seg.start.year, seg.start.month
    while (year, month) <= (last.year, last.month):
        days = monthrange(year, month)[1]
        first = max(seg.start, date(year, month, 1))
        final = min(last, date(year, month, days))
        yield f"{year:04d}-{month:02d}", seg.ratio * Fraction((final - first).days + 1, days)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


class Ledger:
    """按月的技能供给与可计容量台账：同月多段并存，但当月可计总投入上限为 1。"""

    def __init__(self, segments: list[Segment], issues: list[dict[str, Any]]):
        self.segments, self.issues = segments, issues
        self.by_month: dict[str, list[tuple[Segment, Fraction]]] = {}
        self.supply: dict[str, dict[str, Fraction]] = {}
        for seg in segments:
            for month, amount in segment_months(seg):
                self.by_month.setdefault(month, []).append((seg, amount))
                for skill in seg.skills:
                    self.supply.setdefault(skill, {}).setdefault(month, Fraction(0))
                    self.supply[skill][month] += amount
        self.months = sorted(self.by_month)
        self.capacity: dict[str, Fraction] = {}
        for month in self.months:
            total = sum((amount for _, amount in self.by_month[month]), Fraction(0))
            if total > 1:
                issues.append({"code": "month_capped", "month": month,
                               "detail": f"{month} 申报总投入 {total} 超过 1 FTE，按上限 1 计"})
            self.capacity[month] = min(Fraction(1), total)


@dataclass(frozen=True)
class ClauseAudit:
    skill: str
    label: str
    min_months: int
    status: str
    counted: str  # exposure：接触月数；dedicated：该技能的个体可分配上界
    evidence: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"skill": self.label, "min_months": self.min_months, "status": self.status,
                "counted": self.counted, "evidence": list(self.evidence)}


@dataclass(frozen=True)
class QualificationReport:
    contract: str
    verdict: str
    requirement: SkillRequirement
    clauses: tuple[ClauseAudit, ...]
    issues: tuple[dict[str, Any], ...]
    witness: dict[str, Any] | None
    resume: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"contract": self.contract, "verdict": self.verdict,
                "requirement": self.requirement.to_dict(),
                "clauses": [clause.to_dict() for clause in self.clauses],
                "issues": list(self.issues), "witness": self.witness, "resume": self.resume}
