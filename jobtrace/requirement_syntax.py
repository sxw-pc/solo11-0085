"""Restricted synthetic requirement grammar: vocabulary, clauses, and formulas.

The grammar accepts only an explicit skill vocabulary plus AND / OR /
not-required connectors and parentheses.  Anything else that looks like a
requirement is *undecidable*; it is never treated as an empty formula.
This module exposes only the parser and its evidence-bearing formula tree.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

RULES_VERSION = "req-rules-v1"
MAX_SKILLS = 12
DEFAULT_SYNONYMS = (
    ("airflow", "Airflow"), ("c++", "C++"), ("c#", "C#"), ("docker", "Docker"),
    ("golang", "Go"), ("go", "Go"), ("java", "Java"), ("javascript", "JavaScript"),
    ("kafka", "Kafka"), ("kubernetes", "Kubernetes"), ("k8s", "Kubernetes"),
    ("linux", "Linux"), ("python", "Python"), ("spark", "Spark"), ("sql", "SQL"),
    ("typescript", "TypeScript"),
)
@dataclass(frozen=True)
class Rules:
    """Versioned skill/synonym table; a version bump never edits stored text."""
    version: str = RULES_VERSION
    synonyms: tuple[tuple[str, str], ...] = DEFAULT_SYNONYMS
    def aliases(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.synonyms, key=lambda item: (-len(item[0]), item[0])))

DEFAULT_RULES = Rules()
@dataclass(frozen=True)
class Span:
    start: int
    end: int
    def as_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}

@dataclass(frozen=True)
class Clause:
    index: int
    text: str
    span: Span
    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "text": self.text, "span": self.span.as_dict()}

@dataclass(frozen=True)
class Node:
    kind: str
    children: tuple[Node, ...] = ()
    skill: str | None = None
    span: Span | None = None
    clause: int | None = None

TRUE = Node("true")
AND, OR, ATOM, NOTREQ = "and", "or", "atom", "notreq"
_CLAUSE_SPLIT = re.compile(r"[。；;！!\n]+")
_NO_REQ = re.compile(r"(无|不设|没有)(任何|特殊|技能|特定)?要求|不限技能|技能不限|无技能门槛")
_MARKER = re.compile(r"不要求|无需|不需要|必须|必需|需要|要求|必备|掌握|熟悉|具备")
_CHOICE = re.compile(r"(?:任选其一|任选一(?:项)?|任一|之一)\s*$")
_OPS = (
    ("notreq", r"不要求具备|不需要具备|无需具备|不要求|不需要|不必须|无需|不限"),
    ("or", r"或者|或"), ("and", r"并且|同时|而且|且"), ("list", r"、|,|，|与|和"),
    ("modal", r"必须|必需|需要|要求|必备|掌握|熟悉|具备|会使用|会用|会"),
    ("lp", r"\("), ("rp", r"\)"),
)
class _Patterns:
    def __init__(self, rules: Rules):
        aliases = rules.aliases()
        parts, self.names, skills = [], {}, []
        for index, (alias, skill) in enumerate(aliases):
            escaped = re.escape(alias)
            if re.search(r"[A-Za-z]", alias):
                escaped = rf"(?<![A-Za-z0-9+#]){escaped}(?![A-Za-z0-9+#])"
            parts.append(f"(?P<s{index}>{escaped})")
            self.names[f"s{index}"] = (ATOM, skill)
            skills.append(f"(?P<k{index}>{escaped})")
        for name, pattern in _OPS:
            parts.append(f"(?P<o_{name}>{pattern})")
            self.names[f"o_{name}"] = (name, "")
        self.master = re.compile("|".join(parts), re.IGNORECASE)
        self.search = re.compile("|".join(skills), re.IGNORECASE)

_CACHE: dict[tuple[tuple[str, str], ...], _Patterns] = {}
def _patterns(rules: Rules) -> _Patterns:
    key = rules.aliases()
    if key not in _CACHE:
        _CACHE[key] = _Patterns(rules)
    return _CACHE[key]

class _ParseFailure(Exception):
    pass

def _sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def _render(node: Node) -> str:
    if node.kind == ATOM:
        return str(node.skill)
    if node.kind == NOTREQ:
        return f"不要求({_render(node.children[0])})"
    if node.kind == "true":
        return "TRUE"
    separator = " 且 " if node.kind == AND else " 或 "
    return "(" + separator.join(sorted({_render(child) for child in node.children})) + ")"

def _combine(kind: str, nodes: Iterable[Node]) -> Node:
    flat: list[Node] = []
    for node in nodes:
        if node.kind == "true" and kind == AND:
            continue
        flat.extend(node.children if node.kind == kind else (node,))
    unique = {_render(node): node for node in flat}
    ordered = tuple(unique[key] for key in sorted(unique))
    if not ordered:
        return TRUE
    return ordered[0] if len(ordered) == 1 else Node(kind, ordered)

def _atoms(node: Node) -> Iterable[Node]:
    if node.kind == ATOM:
        yield node
    for child in node.children:
        yield from _atoms(child)

def _evaluate(node: Node, skills: frozenset[str]) -> bool:
    if node.kind == ATOM:
        return node.skill in skills
    if node.kind in (NOTREQ, "true"):
        return True
    if node.kind == AND:
        return all(_evaluate(child, skills) for child in node.children)
    return any(_evaluate(child, skills) for child in node.children)

def _node_dict(node: Node) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": node.kind, "render": _render(node)}
    if node.kind == ATOM:
        payload.update({"skill": node.skill, "span": node.span.as_dict() if node.span else None, "clause": node.clause})
    if node.children:
        payload["children"] = [_node_dict(child) for child in node.children]
    return payload

@dataclass(frozen=True)
class RequirementParse:
    status: str
    formula: Node | None
    clauses: tuple[Clause, ...]
    skills: tuple[str, ...]
    rules_version: str
    reason: str | None = None
    description_sha256: str = ""
    @property
    def decidable(self) -> bool:
        return self.status == "OK" and self.formula is not None
    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "formula": _node_dict(self.formula) if self.formula else None,
                "render": _render(self.formula) if self.formula else None,
                "clauses": [clause.as_dict() for clause in self.clauses],
                "skills": list(self.skills), "rules_version": self.rules_version,
                "description_sha256": self.description_sha256}

Token = tuple[str, str, int, int]
def _tokenize(text: str, base: int, rules: Rules) -> list[Token]:
    found = _patterns(rules)
    tokens: list[Token] = []
    position = 0
    for match in found.master.finditer(text):
        if text[position:match.start()].strip(" \t:：-—·"):
            raise _ParseFailure(f"unknown text: {text[position:match.start()]!r}")
        kind, skill = found.names[match.lastgroup or ""]
        tokens.append((kind, skill, base + match.start(), base + match.end()))
        position = match.end()
    if text[position:].strip(" \t:：-—·"):
        raise _ParseFailure(f"unknown text: {text[position:]!r}")
    return [token for token in tokens if token[0] != "modal"]

def _primary(tokens: Sequence[Token], index: int, clause: int) -> tuple[Node, int]:
    if index >= len(tokens):
        raise _ParseFailure("missing operand")
    kind, value, start, end = tokens[index]
    if kind == ATOM:
        return Node(ATOM, skill=value, span=Span(start, end), clause=clause), index + 1
    if kind == "lp":
        node, next_index = _expression(tokens, index + 1, clause)
        if next_index >= len(tokens) or tokens[next_index][0] != "rp":
            raise _ParseFailure("unbalanced parenthesis")
        return node, next_index + 1
    raise _ParseFailure(f"unexpected token: {kind}")

def _unary(tokens: Sequence[Token], index: int, clause: int) -> tuple[Node, int]:
    if index < len(tokens) and tokens[index][0] == "notreq":
        node, next_index = _junction(tokens, index + 1, clause, AND, _primary)
        return Node(NOTREQ, (node,)), next_index
    return _primary(tokens, index, clause)

def _junction(tokens: Sequence[Token], index: int, clause: int, kind: str, operand: Any) -> tuple[Node, int]:
    node, index = operand(tokens, index, clause)
    parts = [node]
    while index < len(tokens) and tokens[index][0] == kind:
        node, index = operand(tokens, index + 1, clause)
        parts.append(node)
    return _combine(kind, parts), index

def _expression(tokens: Sequence[Token], index: int, clause: int) -> tuple[Node, int]:
    node, index = _junction(tokens, index, clause, AND, _unary)
    parts = [node]
    while index < len(tokens) and tokens[index][0] == "or":
        node, index = _junction(tokens, index + 1, clause, AND, _unary)
        parts.append(node)
    return _combine(OR, parts), index

def _parse_clause(clause: Clause, rules: Rules) -> tuple[Node | None, str | None]:
    text = clause.text
    if _NO_REQ.search(text) and not _patterns(rules).search.search(text):
        return TRUE, None
    if not _patterns(rules).search.search(text):
        return None, "requirement marker without a known skill" if _MARKER.search(text) else None
    choice = _CHOICE.search(text)
    body = text[:choice.start()].rstrip("，,、与和 ") if choice else text
    try:
        tokens = _tokenize(body, clause.span.start, rules)
        if not tokens:
            raise _ParseFailure("no tokens")
        if choice:
            tokens = [("or" if kind == "list" else kind, *rest) for kind, *rest in tokens]
        else:
            tokens = [("and" if kind == "list" else kind, *rest) for kind, *rest in tokens]
        node, position = _expression(tokens, 0, clause.index)
        if position != len(tokens):
            raise _ParseFailure("trailing tokens")
    except _ParseFailure as exc:
        return None, f"clause {clause.index}: {exc}"
    if not any(True for _ in _atoms(node)):
        return None, f"clause {clause.index}: no skill atom"
    return node, None

def parse_requirements(text: str | None, rules: Rules = DEFAULT_RULES) -> RequirementParse:
    raw = text or ""
    base = {"rules_version": rules.version, "description_sha256": _sha(text)}
    if not raw.strip():
        return RequirementParse("MISSING", None, (), (), reason="no description text", **base)
    clauses, parsed, errors, explicit_true, start = [], [], [], False, 0
    for match in _CLAUSE_SPLIT.finditer(raw + "\n"):
        end = min(match.start(), len(raw))
        body, start = raw[start:end], match.end()
        if not body.strip():
            continue
        clause = Clause(len(clauses), body, Span(end - len(body), end))
        clauses.append(clause)
        node, error = _parse_clause(clause, rules)
        if error:
            errors.append(error)
        elif node is not None:
            explicit_true = explicit_true or node.kind == "true"
            if node.kind != "true":
                parsed.append(node)
    skills = tuple(sorted({atom.skill for node in parsed for atom in _atoms(node)}))
    if errors:
        return RequirementParse("UNKNOWN", None, tuple(clauses), skills, reason="; ".join(errors), **base)
    if not parsed and not explicit_true:
        return RequirementParse("UNKNOWN", None, tuple(clauses), (), reason="no restricted requirement clause", **base)
    used = {atom.clause for node in parsed for atom in _atoms(node)}
    formula = _combine(AND, parsed) if parsed else TRUE
    return RequirementParse("OK", formula, tuple(c for c in clauses if c.index in used), skills, reason=None, **base)
