from __future__ import annotations
import json
from itertools import product
from typing import Any, Iterable, NamedTuple
from .requirements import (
    Literal, Node, RequirementParse, Ruleset, parse_requirements, render,
)
class Witness(NamedTuple):
    skills: tuple[str, ...]
    clause_indices: tuple[int, ...]
    spans: tuple[tuple[int, int], ...]
    side: str = "old"
    def as_dict(self) -> dict[str, object]:
        return {"skills": list(self.skills), "clauses": list(self.clause_indices),
                "spans": [list(span) for span in self.spans], "side": self.side}
class ImpactComparison(NamedTuple):
    decidable: bool
    reason: str
    ruleset_version: str
    old_formula: str | None
    new_formula: str | None
    old_skills: tuple[str, ...]
    new_skills: tuple[str, ...]
    old_implies_new: bool | None = None
    new_implies_old: bool | None = None
    gained: tuple[Witness, ...] = ()
    lost: tuple[Witness, ...] = ()
    @property
    def qualification_set_changed(self) -> bool:
        return self.decidable and (not self.old_implies_new or not self.new_implies_old)
    def as_dict(self) -> dict[str, object]:
        return {"decidable": self.decidable, "reason": self.reason,
                "ruleset_version": self.ruleset_version,
                "old_formula": self.old_formula, "new_formula": self.new_formula,
                "old_skills": list(self.old_skills), "new_skills": list(self.new_skills),
                "old_implies_new": self.old_implies_new,
                "new_implies_old": self.new_implies_old,
                "qualification_set_changed": self.qualification_set_changed,
                "gained": [w.as_dict() for w in self.gained],
                "lost": [w.as_dict() for w in self.lost]}
def _evaluate(node: Node, present: frozenset[str]) -> bool:
    if isinstance(node, Literal):
        truth = node.skill in present
        return (not truth) if node.negated else truth
    results = (_evaluate(child, present) for child in node.children)
    truth = all(results) if node.op == "AND" else any(results)
    return (not truth) if node.negated else truth
def _all_models(skills: tuple[str, ...]) -> list[frozenset[str]]:
    return [frozenset(s for s, on in zip(skills, bits) if on)
            for bits in product((False, True), repeat=len(skills))]
def _entails(antecedent: Node, consequent: Node, universe: tuple[str, ...]) -> bool:
    return all(not _evaluate(antecedent, m) or _evaluate(consequent, m)
               for m in _all_models(universe))
def _minimal_models(node: Node, universe: tuple[str, ...]) -> list[frozenset[str]]:
    minimal: list[frozenset[str]] = []
    for size in range(len(universe) + 1):
        for bits in product((False, True), repeat=len(universe)):
            if bits.count(True) != size: continue
            model = frozenset(s for s, on in zip(universe, bits) if on)
            if _evaluate(node, model) and not any(model > m for m in minimal):
                minimal.append(model)
    return minimal
def _literals(node: Node) -> list[Literal]:
    if isinstance(node, Literal): return [node]
    return [item for child in node.children for item in _literals(child)]
def _trace(model: frozenset[str], primary: RequirementParse, side: str,
           context: RequirementParse | None) -> Witness:
    clauses, spans = [], []
    for clause in primary.clauses:
        if not _evaluate(clause.node, model): continue
        clauses.append(clause)
        spans.extend((lit.start, lit.end) for lit in _literals(clause.node)
                     if _evaluate(lit, model))
    if context is not None:
        for clause in context.clauses:
            for lit in _literals(clause.node):
                holds, violated = _evaluate(lit, model), not _evaluate(lit, model)
                if (not lit.negated and holds) or (lit.negated and violated):
                    spans.append((lit.start, lit.end))
    clauses.sort(key=lambda item: item.index)
    return Witness(tuple(sorted(model)), tuple(c.index for c in clauses),
                   tuple(sorted(set(spans))), side)
def _difference_witnesses(antecedent: Node, consequent: Node,
                          satisfied: RequirementParse, failed: RequirementParse,
                          side: str, universe: tuple[str, ...]) -> tuple[Witness, ...]:
    counter = [m for m in _minimal_models(antecedent, universe)
               if not _evaluate(consequent, m)]
    if not counter:  # fallback: minimal counterexamples over every model
        for model in _all_models(universe):
            if _evaluate(antecedent, model) and not _evaluate(consequent, model) \
                    and not any(model > other for other in counter):
                counter.append(model)
    ordered = sorted(set(counter), key=lambda item: tuple(sorted(item)))
    return tuple(_trace(m, satisfied, side, failed) for m in ordered)
def compare_requirements(old_text: str | None, new_text: str | None,
                         ruleset: Ruleset | None = None) -> ImpactComparison:
    ruleset = ruleset or Ruleset.default()
    old, new = parse_requirements(old_text, ruleset), parse_requirements(new_text, ruleset)
    if old.undecidable or new.undecidable:
        reason = "; ".join(p for p in (
            f"old: {old.reason}" if old.undecidable else "",
            f"new: {new.reason}" if new.undecidable else "") if p)
        return ImpactComparison(False, reason, ruleset.version,
            render(old.root) if old.root else None,
            render(new.root) if new.root else None, old.skills, new.skills)
    assert old.root is not None and new.root is not None
    universe = tuple(sorted(set(old.skills) | set(new.skills)))
    forward = _entails(old.root, new.root, universe)
    backward = _entails(new.root, old.root, universe)
    gained = () if backward else _difference_witnesses(new.root, old.root, new, old, "new", universe)
    lost = () if forward else _difference_witnesses(old.root, new.root, old, new, "old", universe)
    return ImpactComparison(True, "", ruleset.version, render(old.root), render(new.root),
                            old.skills, new.skills, forward, backward, gained, lost)
class ContractFinding(NamedTuple):
    contract: str
    changed: bool
    kind: str
    detail: str
    comparison: ImpactComparison | None = None
    def as_dict(self) -> dict[str, object]:
        payload = {"contract": self.contract, "changed": self.changed,
                   "kind": self.kind, "detail": self.detail}
        if self.comparison is not None: payload["impact"] = self.comparison.as_dict()
        return payload
def text_audit_contract(old_text: str | None, new_text: str | None,
                        ruleset: Ruleset | None = None) -> ContractFinding:
    if (old_text or "") == (new_text or ""):
        return ContractFinding("text_audit", False, "unchanged", "description text identical")
    return ContractFinding("text_audit", True, "text_revision", "raw description text changed")
def application_impact_contract(old_text: str | None, new_text: str | None,
                                ruleset: Ruleset | None = None) -> ContractFinding:
    comparison = compare_requirements(old_text, new_text, ruleset)
    if not comparison.decidable:
        return ContractFinding("application_impact", False, "undecidable",
                               comparison.reason, comparison)
    if not comparison.qualification_set_changed:
        return ContractFinding("application_impact", False, "equivalent_rewrite",
                               "qualifying skill-set set unchanged", comparison)
    detail = ("requirements tightened" if comparison.old_implies_new
              else "requirements relaxed" if comparison.new_implies_old
              else "requirements diverged")
    return ContractFinding("application_impact", True, "qualification_change",
                           detail, comparison)
def _snapshot_description(row: dict[str, Any]) -> str:
    try:
        return str(json.loads(row["payload_json"]).get("description") or "")
    except (TypeError, json.JSONDecodeError, KeyError):
        return ""
def requirement_report(store: Any, *, site_key: str | None = None,
                       ruleset: Ruleset | None = None) -> dict[str, Any]:
    from .store import Store  # local import keeps the dependency direction one-way
    assert isinstance(store, Store)
    ruleset = ruleset or Ruleset.default()
    entries: list[dict[str, Any]] = []
    for job in store.list_jobs(site_key=site_key, limit=10000):
        rows = store.list_snapshots(job["job_uid"])
        for previous, current in zip(rows, rows[1:]):
            old_text, new_text = _snapshot_description(previous), _snapshot_description(current)
            record = {"text_audit": text_audit_contract(old_text, new_text, ruleset).as_dict(),
                      "application_impact": application_impact_contract(
                          old_text, new_text, ruleset).as_dict(),
                      "impact": compare_requirements(old_text, new_text, ruleset).as_dict()}
            record.update(job_uid=job["job_uid"], external_id=job["external_id"],
                          old_run_id=previous["run_id"], new_run_id=current["run_id"],
                          old_description=old_text, new_description=new_text)
            entries.append(record)
    return {"ruleset_version": ruleset.version, "revisions_scanned": len(entries),
            "text_revisions": sum(1 for i in entries if i["text_audit"]["changed"]),
            "qualification_changes": sum(1 for i in entries if i["application_impact"]["changed"]),
            "equivalent_rewrites": sum(
                1 for i in entries if i["text_audit"]["changed"]
                and not i["application_impact"]["changed"] and i["impact"]["decidable"]),
            "undecidable_revisions": sum(1 for i in entries if not i["impact"]["decidable"]),
            "revisions": entries}
def describe_comparison(entries: Iterable[dict[str, Any]]) -> list[str]:
    lines = []
    for entry in entries:
        impact = entry["impact"]
        if not impact["decidable"]:
            verdict = f"undecidable ({impact['reason']})"
        elif impact["qualification_set_changed"]:
            verdict = (f"qualification change gained={len(impact['gained'])} "
                       f"lost={len(impact['lost'])}")
        else:
            verdict = "equivalent rewrite"
        lines.append(f"{entry['external_id']} {entry['old_run_id']} -> "
                     f"{entry['new_run_id']}: {verdict}")
    return lines
