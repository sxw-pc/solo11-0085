"""Eligibility-impact and text-audit contracts over parsed requirements."""
from __future__ import annotations

import itertools
from typing import Any

from .requirement_syntax import (
    DEFAULT_RULES,
    MAX_SKILLS,
    Node,
    RequirementParse,
    Rules,
    _atoms,
    _evaluate,
    _sha,
    parse_requirements,
)


def _refs(node: Node, parse: RequirementParse,
          skills: set[str] | None = None) -> list[dict[str, Any]]:
    refs, seen = [], set()
    by_index = {clause.index: clause for clause in parse.clauses}
    for atom in _atoms(node):
        if skills is not None and atom.skill not in skills:
            continue
        key = (atom.skill, atom.clause, atom.span.start if atom.span else -1)
        if key in seen:
            continue
        seen.add(key)
        clause = by_index.get(atom.clause) if atom.clause is not None else None
        refs.append({"skill": atom.skill,
                     "atom_span": atom.span.as_dict() if atom.span else None,
                     "clause": atom.clause, "clause_text": clause.text if clause else "",
                     "clause_span": clause.span.as_dict() if clause else None})
    return sorted(refs, key=lambda item: (
        item["clause"] or 0, (item["atom_span"] or {}).get("start", -1), item["skill"]))


def _witness(skills: frozenset[str], mine: RequirementParse,
             other: RequirementParse) -> dict[str, Any]:
    other_skills = {atom.skill for atom in _atoms(other.formula) if other.formula is not None}
    missing = sorted(other_skills - set(skills))
    return {"skills": sorted(skills), "clauses": _refs(mine.formula, mine, set(skills)),
            "other_missing_skills": missing,
            "other_clauses": _refs(other.formula, other, set(missing))}


def _order(skills: frozenset[str]) -> tuple[int, tuple[str, ...]]:
    return len(skills), tuple(sorted(skills))


def impact_audit(old_text: str | None, new_text: str | None,
                 rules: Rules = DEFAULT_RULES) -> dict[str, Any]:
    """Compare the sets of skill bundles that satisfy old and new requirements."""
    old, new = parse_requirements(old_text, rules), parse_requirements(new_text, rules)
    result: dict[str, Any] = {"contract": "impact", "old": old.as_dict(), "new": new.as_dict()}
    undecidable = {"status": "undecidable", "direction": None, "equivalent": None,
                   "old_implies_new": None, "new_implies_old": None,
                   "gained": None, "lost": None}
    if not old.decidable or not new.decidable:
        result.update(undecidable)
        result["reason"] = f"old={old.status}, new={new.status}; no empty-formula assumption"
        return result
    domain = sorted({atom.skill for formula in (old.formula, new.formula)
                     for atom in _atoms(formula) if formula is not None})
    if len(domain) > MAX_SKILLS:
        result.update(undecidable)
        result["reason"] = "skill domain exceeds bounded analysis"
        return result
    assignments = [frozenset(combo) for size in range(len(domain) + 1)
                   for combo in itertools.combinations(domain, size)]
    old_sat = {s for s in assignments if old.formula and _evaluate(old.formula, s)}
    new_sat = {s for s in assignments if new.formula and _evaluate(new.formula, s)}
    gained, lost = new_sat - old_sat, old_sat - new_sat
    if not gained and not lost:
        status, direction = "no_substantive_change", "equivalent"
    elif not lost:
        status, direction = "eligibility_changed", "relax"
    elif not gained:
        status, direction = "eligibility_changed", "tighten"
    else:
        status, direction = "eligibility_changed", "mixed"
    result.update({
        "status": status, "direction": direction,
        "equivalent": status == "no_substantive_change",
        "old_implies_new": old_sat <= new_sat, "new_implies_old": new_sat <= old_sat,
        "gained": _witness(min(gained, key=_order), new, old) if gained else None,
        "lost": _witness(min(lost, key=_order), old, new) if lost else None,
        "reason": None,
    })
    return result


def text_audit(old_text: str | None, new_text: str | None,
               rules: Rules = DEFAULT_RULES) -> dict[str, Any]:
    """Record any byte-level description change, even an equivalent rewrite."""
    changed = (old_text or "") != (new_text or "")
    return {"contract": "text_audit", "status": "text_revised" if changed else "unchanged",
            "description_changed": changed, "old_description": old_text or "",
            "new_description": new_text or "", "old_sha256": _sha(old_text),
            "new_sha256": _sha(new_text), "rules_version": rules.version}


def audit_revision(old_text: str | None, new_text: str | None,
                   rules: Rules = DEFAULT_RULES) -> dict[str, Any]:
    action = "text_revision" if (old_text or "") != (new_text or "") else "none"
    return {"action": action, "text_audit": text_audit(old_text, new_text, rules),
            "impact": impact_audit(old_text, new_text, rules)}


def revision_action(old_text: str | None, new_text: str | None,
                    old_rules_version: str, new_rules_version: str) -> str:
    if (old_text or "") != (new_text or ""):
        return "text_revision"
    return "recompute_derived" if old_rules_version != new_rules_version else "none"
