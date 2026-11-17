"""Charset acceptance: evidence-based decoding before any field becomes a job.

Two explicit collection contracts are supported:

* ``strict``    - only an explicitly declared HTTP charset that decodes
                  losslessly is trusted; a mislabelled header fails the page.
* ``reconcile`` - when the HTTP declaration cannot decode losslessly, a valid
                  in-document declaration (or BOM) may override it, provided
                  the candidate field text is free of unexplained loss.

Decisions always work from the original response bytes. Replacement-decoded
text is never re-encoded to guess the source bytes.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup, Tag

from .errors import JobTraceError
from .fingerprint import canonical_json
from .normalize import clean_text

RULE_VERSION = "charset-1"
CONTRACTS = ("strict", "reconcile")
REPLACEMENT_CHAR = "�"
# Latin-1/ASCII map bytes 1:1 and would "decode" anything; they cannot confirm
# CJK field bytes, so they never count as field-bearing evidence.
NON_EVIDENCE_ENCODINGS = {"ascii", "iso8859-1", "latin-1", "iso-8859-1"}
_META_CHARSET = re.compile(
    rb"""<meta[^>]+?charset\s*=\s*["']?\s*([0-9a-zA-Z][\w:-]{0,40})""",
    re.IGNORECASE,
)
_HTTP_CHARSET = re.compile(r"charset=([\w.-]+)", re.IGNORECASE)
_BOMS = (
    ("utf-32", codecs.BOM_UTF32), ("utf-32-le", codecs.BOM_UTF32_LE),
    ("utf-32-be", codecs.BOM_UTF32_BE), ("utf-16", codecs.BOM_UTF16),
    ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-8-sig", codecs.BOM_UTF8),
)


class CharsetError(JobTraceError):
    code = "charset_contract_error"


@dataclass(frozen=True)
class FieldSpan:
    """One field occurrence: text from a decoded candidate and its byte range."""

    name: str
    text: str
    byte_start: int | None
    byte_end: int | None
    card: int | None = None


@dataclass(frozen=True)
class DecodeCandidate:
    encoding: str
    source: str  # http | meta | bom | default
    strict_ok: bool
    lossless: bool
    has_replacement: bool
    fields: tuple[FieldSpan, ...] = ()


@dataclass(frozen=True)
class CharsetDecision:
    sha256: str
    url: str
    kind: str
    contract: str
    rule_version: str
    http_charset: str | None
    meta_charset: str | None
    bom_encoding: str | None
    accepted: bool
    ambiguous: bool
    accepted_encoding: str | None
    accepted_source: str | None
    reason: str
    candidates: tuple[DecodeCandidate, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def canonical(self) -> str:
        return canonical_json(self.as_dict())


def lookup_name(declared: str | None) -> str | None:
    if not declared:
        return None
    try:
        return codecs.lookup(declared).name
    except LookupError:
        return None


def http_charset(content_type: str) -> str | None:
    match = _HTTP_CHARSET.search(content_type or "")
    return match.group(1) if match else None


def detect_bom(content: bytes) -> str | None:
    for name, marker in _BOMS:
        if content.startswith(marker):
            return name
    return None


def meta_charset(content: bytes) -> str | None:
    """Prescan the raw head bytes; never derived from replacement text."""

    match = _META_CHARSET.search(content[:4096])
    if not match:
        return None
    try:
        declared = match.group(1).decode("ascii")
    except UnicodeDecodeError:
        return None
    return lookup_name(declared) or declared


def _field_spans(content: bytes, text: str, encoding: str,
                 selectors: object, kind: str) -> tuple[FieldSpan, ...]:
    """Decode per candidate, run selectors, then locate each value in raw bytes."""

    soup = BeautifulSoup(text, "html.parser")
    targets: list[tuple[str, Tag, int | None]] = []
    if kind == "list":
        for index, card in enumerate(soup.select(selectors.list.item), start=1):
            for name, selector in (("title", selectors.list.title),
                                   ("company", selectors.list.company)):
                node = card.select_one(selector)
                if node is not None:
                    targets.append((name, node, index))
    else:
        for name, selector in (("title", selectors.detail.title),
                               ("company", selectors.detail.company)):
            node = soup.select_one(selector)
            if node is not None:
                targets.append((name, node, None))
    spans: list[FieldSpan] = []
    span_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
    for name, node, card in targets:
        raw_text = node.get_text(" ", strip=True)
        value = clean_text(raw_text)
        start = None
        if raw_text:
            try:
                raw = raw_text.encode(span_encoding)
            except UnicodeEncodeError:
                raw = b""
            offset = content.find(raw) if raw else -1
            if offset >= 0:
                start = offset
        spans.append(FieldSpan(name, value, start,
                               start + len(raw) if start is not None else None, card))
    return tuple(spans)


def _candidate(content: bytes, encoding: str, source: str,
               selectors: object, kind: str) -> DecodeCandidate:
    try:
        text = content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return DecodeCandidate(encoding, source, False, False, False)
    try:
        if encoding == "utf-8-sig":
            # The BOM is transport framing; the payload itself must round-trip.
            lossless = codecs.BOM_UTF8 + text.encode("utf-8") == content
        else:
            lossless = text.encode(encoding) == content
    except UnicodeEncodeError:
        lossless = False
    return DecodeCandidate(
        encoding=encoding, source=source, strict_ok=True, lossless=lossless,
        has_replacement=REPLACEMENT_CHAR in text,
        fields=_field_spans(content, text, encoding, selectors, kind),
    )


def _build_candidates(content: bytes, head: str | None, meta: str | None,
                      bom: str | None, selectors: object,
                      kind: str) -> list[DecodeCandidate]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(encoding: str | None, source: str) -> None:
        name = lookup_name(encoding)
        if not name or name in seen:
            return
        seen.add(name)
        ordered.append((name, source))

    add(bom, "bom")
    add(head, "http")
    add(meta, "meta")
    add("utf-8", "default")
    return [_candidate(content, name, source, selectors, kind)
            for name, source in ordered]


def _lossless(decision: list[DecodeCandidate], kind: str) -> list[DecodeCandidate]:
    candidates = [
        item for item in decision
        if item.strict_ok and item.lossless and not item.has_replacement
    ]
    if kind == "detail":
        candidates = [item for item in candidates
                      if item.encoding not in NON_EVIDENCE_ENCODINGS]
    return candidates


def _field_values(candidate: DecodeCandidate, kind: str) -> tuple[str, str] | None:
    if kind != "detail" or candidate.encoding in NON_EVIDENCE_ENCODINGS:
        return None
    title = next((span.text for span in candidate.fields if span.name == "title"), "")
    company = next((span.text for span in candidate.fields if span.name == "company"), "")
    if not title or not company:
        return None
    return title, company


def _ambiguous(candidates: list[DecodeCandidate], kind: str) -> bool:
    """Two lossless interpretations yielding different required field values."""

    values = {_field_values(candidate, kind) for candidate in candidates}
    values.discard(None)
    return len(values) > 1


def analyze(content: bytes, *, sha256: str, url: str, kind: str,
            content_type: str, contract: str,
            selectors: object | None = None) -> CharsetDecision:
    if contract not in CONTRACTS:
        raise CharsetError(f"unknown charset contract: {contract!r}")
    head_name = lookup_name(http_charset(content_type))
    meta_name = meta_charset(content)
    bom_name = detect_bom(content)
    candidates = _build_candidates(
        content, head_name, meta_name, bom_name, selectors, kind
    )
    good = _lossless(candidates, kind)
    ambiguous = _ambiguous(good, kind)

    def decision(accepted: bool, encoding: str | None, source: str | None,
                 reason: str) -> CharsetDecision:
        return CharsetDecision(
            sha256=sha256, url=url, kind=kind, contract=contract,
            rule_version=RULE_VERSION, http_charset=head_name,
            meta_charset=meta_name, bom_encoding=bom_name, accepted=accepted,
            ambiguous=ambiguous, accepted_encoding=encoding,
            accepted_source=source, reason=reason, candidates=tuple(candidates),
        )

    if ambiguous:
        return decision(False, None, None,
                        "ambiguous: lossless declarations yield different field values")
    http = next((candidate for candidate in good if candidate.source == "http"), None)
    if http is not None:
        return decision(True, http.encoding, "http", "http charset decoded losslessly")
    if contract == "strict":
        return decision(False, None, None,
                        "strict contract: http charset missing or not losslessly decodable")
    for source in ("bom", "meta"):
        candidate = next((item for item in good if item.source == source), None)
        if candidate is not None:
            head_state = "non-decodable" if head_name else "missing"
            return decision(
                True, candidate.encoding, source,
                f"{source} declaration overrides {head_state} http charset",
            )
    return decision(False, None, None,
                    "reconcile contract: no lossless document/bom evidence available")


def accepted_text(content: bytes, decision: CharsetDecision) -> str:
    if not decision.accepted or not decision.accepted_encoding:
        raise CharsetError(f"no accepted encoding for {decision.url}")
    return content.decode(decision.accepted_encoding)
