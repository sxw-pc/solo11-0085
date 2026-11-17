from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import NamedTuple
MAX_SKILLS, MAX_CLAUSES = 12, 32
RULESET_VERSION = "rules-2026.1"
DEFAULT_SKILLS: tuple[str, ...] = ("Python", "SQL", "Java", "Go", "Airflow", "Spark", "Kubernetes", "Docker", "Linux", "React", "TypeScript", "Kafka")
DEFAULT_SYNONYMS: dict[str, tuple[str, ...]] = {"Python": ("python", "Python3"), "Kubernetes": ("k8s",), "TypeScript": ("TS",)}
_AND = frozenset({"且", "并且", "和"})
_OR = frozenset({"或", "或者", "任选一", "任一"})
_NOT = frozenset({"不要求", "不需要", "无需"})
_DELIMITERS = frozenset({";", "；", ",", "，", "、"})
_FRAME_PUNCT = frozenset({":", "："})
_FRAMING = frozenset({"岗位要求", "任职要求", "技能要求", "要求"})
_FULLWIDTH = {"（": "(", "）": ")", "；": ";", "，": ",", "、": "、", "：": ":"}
class RequirementSyntaxError(ValueError): ...
class Literal(NamedTuple):
    skill: str
    negated: bool
    start: int
    end: int
class Group(NamedTuple):
    op: str
    children: tuple["Node", ...]
    start: int
    end: int
    negated: bool = False
Node = Literal | Group
class Token(NamedTuple):
    kind: str
    text: str
    start: int
    end: int
    skill: str | None = None
class Clause(NamedTuple):
    index: int
    node: Node
    start: int
    end: int
    text: str
class RequirementParse(NamedTuple):
    text: str
    decidable: bool
    ruleset_version: str
    root: Node | None = None
    clauses: tuple[Clause, ...] = ()
    unknown_spans: tuple[tuple[int, int], ...] = ()
    reason: str = ""
    skills: tuple[str, ...] = ()
    @property
    def undecidable(self) -> bool: return not self.decidable
class Ruleset(NamedTuple):
    version: str
    skills: tuple[str, ...]
    synonyms: dict[str, tuple[str, ...]]
    @classmethod
    def default(cls) -> "Ruleset":
        return cls(RULESET_VERSION, DEFAULT_SKILLS, dict(DEFAULT_SYNONYMS))
def normalize_width(text: str) -> tuple[str, list[int]]:
    chars, positions = [], []
    for index, char in enumerate(text):
        for offset, part in enumerate(_FULLWIDTH.get(char, char)):
            chars.append(part)
            positions.append(index + offset)
    return "".join(chars), positions
def skill_pattern(ruleset: Ruleset) -> re.Pattern[str]:
    surfaces = sorted({s for skill in ruleset.skills
                       for s in (skill, *ruleset.synonyms.get(skill, ())) if s},
                      key=len, reverse=True)
    return re.compile("|".join(re.escape(item) for item in surfaces))
def _is_identifier(surface: str) -> bool:
    return bool(surface) and all(ch.isalnum() or ch == "_" for ch in surface)
def _word_boundary(source: str, start: int, end: int) -> bool:
    before = source[start - 1].isalnum() if start > 0 else False
    after = source[end].isalnum() if end < len(source) else False
    return not before and not after
def canonical_skill(surface: str, ruleset: Ruleset) -> str:
    lowered = surface.lower()
    for skill in ruleset.skills:
        accepted = {item.lower() for item in (skill, *ruleset.synonyms.get(skill, ()))}
        if surface == skill or lowered in accepted: return skill
    return surface  # pragma: no cover - pattern only emits known surfaces
@dataclass
class Lexer:
    text: str
    normalized: str
    positions: list[int]
    ruleset: Ruleset
    tokens: list[Token] = field(default_factory=list)
    unknown_spans: list[tuple[int, int]] = field(default_factory=list)
    def _emit(self, kind: str, start: int, end: int, skill: str | None = None) -> None:
        lo, hi = self.positions[start], self.positions[end - 1] + 1
        self.tokens.append(Token(kind, self.text[lo:hi], lo, hi, skill))
    def _grammar_here(self, source: str, index: int, words: re.Pattern[str]) -> bool:
        char = source[index]
        if char.isspace() or char in "()" or char in _DELIMITERS or char in _FRAME_PUNCT:
            return True
        if any(source.startswith(word, index)
               for group in (_NOT, _OR, _AND, _FRAMING) for word in group):
            return True
        match = words.match(source, index)
        return match is not None and (
            _word_boundary(source, index, match.end()) or not _is_identifier(match.group(0)))
    def run(self) -> None:
        source, index, words = self.normalized, 0, skill_pattern(self.ruleset)
        while index < len(source):
            char = source[index]
            if char.isspace():
                index += 1
            elif char in _DELIMITERS or char in _FRAME_PUNCT:
                self._emit("DELIM" if char in _DELIMITERS else "FRAME", index, index + 1)
                index += 1
            else:
                index = self._lex_word(source, index, words)
    def _lex_word(self, source: str, index: int, words: re.Pattern[str]) -> int:
        if source[index] in "()":
            self._emit("LP" if source[index] == "(" else "RP", index, index + 1)
            return index + 1
        for label, kind in ((_NOT, "NOT"), (_OR, "OR"), (_AND, "AND"), (_FRAMING, "FRAME")):
            for word in sorted(label, key=len, reverse=True):
                if source.startswith(word, index):
                    self._emit(kind, index, index + len(word))
                    return index + len(word)
        match = words.match(source, index)
        if match and (_word_boundary(source, index, match.end())
                      or not _is_identifier(match.group(0))):
            self._emit("SKILL", index, match.end(),
                       canonical_skill(match.group(0), self.ruleset))
            return match.end()
        stop = index + 1  # unknown prose: merge chunk until the next grammar point
        while stop < len(source) and not self._grammar_here(source, stop, words): stop += 1
        lo, hi = self.positions[index], self.positions[stop - 1] + 1
        self.unknown_spans.append((lo, hi))
        self.tokens.append(Token("UNKNOWN", self.text[lo:hi], lo, hi))
        return stop
