from __future__ import annotations
from .lexer import (MAX_CLAUSES, MAX_SKILLS, Clause, Group, Lexer, Literal, Node, RequirementParse, RequirementSyntaxError, Ruleset, Token, normalize_width,)
class Parser:
    def __init__(self, tokens: list[Token], text: str): self.tokens, self.text, self.index = tokens, text, 0
    def _peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None
    def _take(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token
    def _expect(self, kind: str) -> Token:
        token = self._peek()
        if token is None or token.kind != kind:
            raise RequirementSyntaxError(
                f"expected {kind} at {token.start if token else len(self.text)}")
        return self._take()
    def parse_clause(self) -> Node:
        node = self._parse_or()
        token = self._peek()
        if token is not None:
            raise RequirementSyntaxError(f"unexpected token {token.text!r} at {token.start}")
        return node
    def _chain(self, kind: str, sub: str) -> Node:
        children = [getattr(self, sub)()]
        while self._peek() is not None and self._peek().kind == kind:
            self._take()
            children.append(getattr(self, sub)())
        return children[0] if len(children) == 1 else Group(
            kind, tuple(children), children[0].start, children[-1].end)
    def _parse_or(self) -> Node: return self._chain("OR", "_parse_and")
    def _parse_and(self) -> Node:
        children = [self._parse_unary()]
        while self._peek() is not None and self._peek().kind == "AND":
            self._take()
            children.append(self._parse_unary())
        while self._starts_primary():  # implicit conjunction, e.g. "Python SQL"
            children.append(self._parse_unary())
        return children[0] if len(children) == 1 else Group(
            "AND", tuple(children), children[0].start, children[-1].end)
    def _starts_primary(self) -> bool:
        token = self._peek()
        return token is not None and token.kind in {"SKILL", "LP", "FRAME", "NOT"}
    def _parse_unary(self) -> Node:
        token = self._peek()
        if token is None or token.kind != "NOT": return self._parse_primary()
        self._take()
        operand = self._parse_unary()
        if isinstance(operand, Literal):
            return Literal(operand.skill, not operand.negated, token.start, operand.end)
        return Group(operand.op, operand.children, operand.start, operand.end,
                     negated=not operand.negated)
    def _parse_primary(self) -> Node:
        token = self._peek()
        if token is None: raise RequirementSyntaxError("dangling connector")
        if token.kind == "FRAME":
            self._take()
            if self._peek() is None: raise RequirementSyntaxError("empty framing label")
            return self._parse_unary()
        if token.kind == "SKILL":
            self._take()
            return Literal(token.skill or token.text, False, token.start, token.end)
        if token.kind == "LP": return self._parse_group(token)
        raise RequirementSyntaxError(f"unsupported text {token.text!r} at {token.start}")
    def _parse_group(self, token: Token) -> Node:
        self._take()
        inner, depth = [], 1
        while self._peek() is not None:
            current = self._take()
            if current.kind == "LP":
                depth += 1
            elif current.kind == "RP":
                depth -= 1
                if depth == 0:
                    close = current
                    break
            inner.append(current)
        else:
            raise RequirementSyntaxError("unbalanced parenthesis")
        operators = {item.kind for item in inner if item.kind in {"AND", "OR"}}
        if operators == {"AND", "OR"}:
            raise RequirementSyntaxError("mixed 且/或 without parentheses is ambiguous")
        node = Parser(inner, self.text).parse_clause()
        children = node.children if isinstance(node, Group) else (node,)
        return Group("OR" if operators == {"OR"} else "AND", tuple(children),
                     token.start, close.end)
def split_clauses(tokens: list[Token]) -> list[list[Token]]:
    chunks, current, depth = [], [], 0
    for token in tokens:
        if token.kind == "LP":
            depth += 1; current.append(token)
        elif token.kind == "RP":
            depth -= 1
            if depth < 0: raise RequirementSyntaxError("unbalanced parenthesis")
            current.append(token)
        elif token.kind == "DELIM" and depth == 0:
            if current: chunks.append(current); current = []
        else:
            current.append(token)
    if depth != 0: raise RequirementSyntaxError("unbalanced parenthesis")
    if current: chunks.append(current)
    return [chunk for chunk in chunks if chunk]
def parse_requirements(text: str | None, ruleset: Ruleset | None = None) -> RequirementParse:
    ruleset = ruleset or Ruleset.default()
    if not text or not text.strip():
        return RequirementParse(text or "", False, ruleset.version,
                                reason="missing or empty description")
    if len(text) > 4000:
        return RequirementParse(text, False, ruleset.version, reason="description too long")
    normalized, positions = normalize_width(text)
    lexer = Lexer(text, normalized, positions, ruleset)
    try:
        lexer.run()
        chunks = split_clauses(lexer.tokens)
        if len(chunks) > MAX_CLAUSES: raise RequirementSyntaxError("clause budget exceeded")
        clauses, skills = [], set()
        for index, chunk in enumerate(chunks):
            node = Parser(chunk, text).parse_clause()
            _collect_skills(node, skills)
            clauses.append(Clause(index, node, chunk[0].start, chunk[-1].end,
                                  text[chunk[0].start:chunk[-1].end]))
        if not skills: raise RequirementSyntaxError("no skill requirement recognized")
        if len(skills) > MAX_SKILLS:
            raise RequirementSyntaxError("skill universe exceeds bounded decision limit")
        root = clauses[0].node if len(clauses) == 1 else Group(
            "AND", tuple(clause.node for clause in clauses),
            clauses[0].start, clauses[-1].end)
        return RequirementParse(text, True, ruleset.version, root=root,
                                clauses=tuple(clauses),
                                unknown_spans=tuple(lexer.unknown_spans),
                                skills=tuple(sorted(skills)))
    except RequirementSyntaxError as exc:
        return RequirementParse(text, False, ruleset.version,
                                unknown_spans=tuple(lexer.unknown_spans), reason=str(exc))
def _collect_skills(node: Node, skills: set[str]) -> None:
    if isinstance(node, Literal):
        skills.add(node.skill)
    else:
        for child in node.children: _collect_skills(child, skills)
def render(node: Node) -> str:
    if isinstance(node, Literal): return ("不要求 " if node.negated else "") + node.skill
    parts = [render(child) for child in node.children]
    inner = (" 或 " if node.op == "OR" else " 且 ").join(parts)
    wrapped = inner if len(parts) == 1 else f"({inner})"
    return ("不要求 " if node.negated else "") + wrapped
