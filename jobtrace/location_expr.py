"""Restricted synthetic location expressions with auditable source spans.

Grammar (restricted, offline; no geocoding and no real personal locations):

    expr    := remote | onsite
    remote  := [scope] "远程" [ "(" constraints ")" ]
    onsite  := place [ ("/" | "+") place ... ] [ "(" constraints ")" ]
    place   := city [ "·" district ]

Constraint fragments inside one trailing parenthesised group, comma separated:

    "任选一" / "任选其一"     -> choose exactly one onsite city (with "/")
    "轮转" / "均需出勤"       -> every city requires attendance (with "+")
    "周" N "天到岗"           -> hybrid: at least N onsite days per week
    "每地每月" N "周"         -> rotation: N weeks per month in each city
    ["须"] "每月赴" city N "天" -> monthly visit of N days to a city

A "/" or "+" group without its disambiguating keyword is *ambiguous*: the
parser never guesses. N is a digit run or a Chinese numeral. All spans are
[start, end) offsets into ``LocationExpr.raw`` (already cleaned text), so
every segment of the tree can be traced back to the original posting.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

RULES_VERSION = "location-rules/1"
WEEKS_PER_MONTH = 4
WORKDAYS_PER_WEEK = 5


@dataclass(frozen=True)
class CityEntry:
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    districts: tuple[str, ...] = ()


@dataclass(frozen=True)
class CityTable:
    version: str
    entries: tuple[CityEntry, ...]
    scopes: tuple[tuple[str, str], ...] = ()

    def lookup(self, name: str) -> CityEntry | None:
        for entry in self.entries:
            if name == entry.name or name in entry.aliases:
                return entry
        return None

    def scope_code(self, label: str) -> str | None:
        return dict(self.scopes).get(label)


def _city(code: str, name: str, aliases: tuple[str, ...], districts: tuple[str, ...]) -> CityEntry:
    return CityEntry(code=code, name=name, aliases=aliases, districts=districts)


_BASE_CITIES = (
    _city("BJ", "北京", ("北京市",), ("朝阳", "海淀")),
    _city("SH", "上海", ("上海市",), ("浦东", "徐汇")),
    _city("GZ", "广州", ("广州市",), ("天河",)),
    _city("SZ", "深圳", ("深圳市",), ("南山",)),
    _city("HZ", "杭州", ("杭州市",), ("西湖",)),
    _city("NJ", "南京", ("南京市",), ("建邺",)),
    _city("CD", "成都", ("成都市",), ("高新",)),
)
_SCOPES = (("全国", "CN-ALL"),)

TABLE_V1 = CityTable(version="synthetic-cities/1", entries=_BASE_CITIES, scopes=_SCOPES)

CITY_TABLE = CityTable(
    version="synthetic-cities/2",
    entries=(
        _city("BJ", "北京", ("北京市", "京"), ("朝阳", "海淀")),
        _city("SH", "上海", ("上海市", "沪"), ("浦东", "徐汇")),
        *_BASE_CITIES[2:],
        _city("CC", "长春", ("长春市",), ("朝阳",)),
    ),
    scopes=_SCOPES,
)


@dataclass(frozen=True)
class SiteNode:
    city: str
    city_code: str | None
    district: str | None
    district_registered: bool
    city_known: bool
    span: tuple[int, int]
    node: str = "site"


@dataclass(frozen=True)
class ChoiceNode:
    options: tuple[SiteNode, ...]
    span: tuple[int, int]
    keyword_span: tuple[int, int] | None = None
    node: str = "choice"


@dataclass(frozen=True)
class AllNode:
    children: tuple[SiteNode, ...]
    rotation_weeks_per_month: int | None
    span: tuple[int, int]
    keyword_span: tuple[int, int] | None = None
    node: str = "all"


@dataclass(frozen=True)
class RemoteNode:
    scope: str | None
    scope_code: str | None
    span: tuple[int, int]
    node: str = "remote"


@dataclass(frozen=True)
class Constraint:
    kind: str  # min_days_per_week | monthly_visit | rotation
    days: int
    span: tuple[int, int]
    city: str | None = None
    city_code: str | None = None


@dataclass(frozen=True)
class LocationExpr:
    raw: str
    kind: str  # empty | site | choice | all | remote | ambiguous
    root: SiteNode | ChoiceNode | AllNode | RemoteNode | None
    constraints: tuple[Constraint, ...] = ()
    sites: tuple[SiteNode, ...] = ()  # flat segments kept for ambiguous parses
    ambiguous: bool = False
    ambiguity_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    rules_version: str = RULES_VERSION
    table_version: str = CITY_TABLE.version

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> LocationExpr:
        root = _node_from_dict(data["root"]) if data.get("root") else None
        constraints = tuple(
            Constraint(kind=c["kind"], days=c["days"], span=tuple(c["span"]),
                       city=c.get("city"), city_code=c.get("city_code"))
            for c in data.get("constraints", ())
        )
        sites = tuple(_node_from_dict(s) for s in data.get("sites", ()))
        return cls(
            raw=data["raw"], kind=data["kind"], root=root, constraints=constraints,
            sites=sites, ambiguous=bool(data.get("ambiguous")),
            ambiguity_reasons=tuple(data.get("ambiguity_reasons", ())),
            notes=tuple(data.get("notes", ())),
            rules_version=data.get("rules_version", RULES_VERSION),
            table_version=data.get("table_version", ""),
        )


def _node_from_dict(data: dict):
    kind = data["node"]
    span = tuple(data["span"])
    keyword = tuple(data["keyword_span"]) if data.get("keyword_span") else None
    if kind == "site":
        return SiteNode(city=data["city"], city_code=data.get("city_code"),
                        district=data.get("district"),
                        district_registered=bool(data.get("district_registered")),
                        city_known=bool(data.get("city_known")), span=span)
    if kind == "choice":
        return ChoiceNode(options=tuple(_node_from_dict(o) for o in data["options"]),
                          span=span, keyword_span=keyword)
    if kind == "all":
        return AllNode(children=tuple(_node_from_dict(c) for c in data["children"]),
                       rotation_weeks_per_month=data.get("rotation_weeks_per_month"),
                       span=span, keyword_span=keyword)
    if kind == "remote":
        return RemoteNode(scope=data.get("scope"), scope_code=data.get("scope_code"), span=span)
    raise ValueError(f"unknown node kind: {kind}")


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_PAREN = re.compile(r"[（(](?P<body>[^）)]*)[）)]")
_WEEKLY = re.compile(r"每?周(?P<n>[0-9]+|[零一二两三四五六七八九十]+)天(?:到岗)?")
_VISIT = re.compile(r"(?:须)?每月赴(?P<city>.+?)(?P<n>[0-9]+|[零一二两三四五六七八九十]+)天")
_ROTATION = re.compile(r"每地每月(?P<n>[0-9]+|[零一二两三四五六七八九十]+)周")
_CHOICE_KEYWORDS = ("任选一", "任选其一")
_ALL_KEYWORDS = ("轮转", "均需出勤")
_SEPARATOR = re.compile(r"[/+]")
_PLACE_SPLIT = re.compile(r"[·•]")


def _parse_num(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    if len(text) == 2 and text[0] == "十":
        return 10 + _CN_NUM.get(text[1], 0)
    if len(text) == 2 and text[1] == "十":
        return _CN_NUM.get(text[0], 0) * 10
    if len(text) == 3 and text[1] == "十":
        return _CN_NUM.get(text[0], 0) * 10 + _CN_NUM.get(text[2], 0)
    return None


def _district_key(name: str) -> str:
    for suffix in ("新区", "区", "县"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _parse_place(segment: str, offset: int, table: CityTable, notes: list[str]) -> SiteNode:
    lead = len(segment) - len(segment.lstrip())
    text = segment.strip()
    span = (offset + lead, offset + lead + len(text))
    parts = _PLACE_SPLIT.split(text, maxsplit=1)
    city_text = parts[0].strip()
    district = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    entry = table.lookup(city_text)
    if entry is None:
        notes.append(f"unknown_city:{city_text}")
        return SiteNode(city=city_text, city_code=None, district=district,
                        district_registered=False, city_known=False, span=span)
    registered = False
    if district:
        known = {_district_key(name) for name in entry.districts}
        registered = _district_key(district) in known
        if not registered:
            notes.append(f"unregistered_district:{district}@{entry.name}")
    return SiteNode(city=entry.name, city_code=entry.code, district=district,
                    district_registered=registered, city_known=True, span=span)


def _constraint(fragment: str, span: tuple[int, int], table: CityTable,
                notes: list[str]) -> Constraint | str | None:
    weekly = _WEEKLY.fullmatch(fragment)
    if weekly:
        days = _parse_num(weekly.group("n"))
        return Constraint("min_days_per_week", days, span) if days is not None else None
    visit = _VISIT.fullmatch(fragment)
    if visit:
        city = visit.group("city").strip()
        entry = table.lookup(city)
        if entry is None:
            notes.append(f"unknown_visit_city:{city}")
        days = _parse_num(visit.group("n"))
        if days is None:
            return None
        return Constraint("monthly_visit", days, span, city=city,
                          city_code=entry.code if entry else None)
    rotation = _ROTATION.fullmatch(fragment)
    if rotation:
        weeks = _parse_num(rotation.group("n"))
        return Constraint("rotation", weeks, span) if weeks is not None else None
    if any(key in fragment for key in _CHOICE_KEYWORDS):
        return "choice"
    if any(key in fragment for key in _ALL_KEYWORDS):
        return "all"
    notes.append(f"unparsed_constraint:{fragment}")
    return None


def parse_location_expr(raw: str, *, table: CityTable = CITY_TABLE,
                        rules_version: str = RULES_VERSION) -> LocationExpr:
    """Parse cleaned text; spans index into the returned ``raw`` field."""

    text = raw.strip()
    notes: list[str] = []
    ambiguity: list[str] = []
    constraints: list[Constraint] = []
    choice_mark: tuple[int, int] | None = None
    all_mark: tuple[int, int] | None = None
    head = text
    match = _PAREN.search(text)
    if match:
        head = text[: match.start()].strip()
        if text[match.end():].strip():
            notes.append("trailing_text_after_constraints")
        body_base = match.start("body")
        for frag_match in re.finditer(r"[^,，、；;]+", match.group("body")):
            piece = frag_match.group(0)
            lead = len(piece) - len(piece.lstrip())
            fragment = piece.strip()
            if not fragment:
                continue
            start = body_base + frag_match.start() + lead
            result = _constraint(fragment, (start, start + len(fragment)), table, notes)
            if isinstance(result, Constraint):
                constraints.append(result)
            elif result == "choice":
                choice_mark = (start, start + len(fragment))
            elif result == "all":
                all_mark = (start, start + len(fragment))
    rotation = next((c for c in constraints if c.kind == "rotation"), None)
    root = None
    sites: list[SiteNode] = []
    kind = "empty"
    if not head:
        pass
    elif "远程" in head:
        index = head.index("远程")
        scope = head[:index].strip() or None
        scope_code = table.scope_code(scope) if scope else None
        if scope and scope_code is None:
            notes.append(f"unknown_scope:{scope}")
        if head[index + 2:].strip():
            notes.append("remote_trailing_text")
        root = RemoteNode(scope=scope, scope_code=scope_code, span=(0, len(head)))
        kind = "remote"
    else:
        seps = [m for m in _SEPARATOR.finditer(head)]
        if not seps:
            root = _parse_place(head, 0, table, notes)
            kind = "site"
        else:
            if len({m.group(0) for m in seps}) > 1:
                ambiguity.append("mixed_separators")
            sep = seps[0].group(0)
            bounds = [0] + [m.start() for m in seps] + [len(head)]
            sites = [
                _parse_place(head[bounds[i]:bounds[i + 1]], bounds[i], table, notes)
                for i in range(len(bounds) - 1) if head[bounds[i]:bounds[i + 1]].strip()
            ]
            if choice_mark and not all_mark and sep == "/":
                root = ChoiceNode(options=tuple(sites), span=(0, len(head)),
                                  keyword_span=choice_mark)
                kind = "choice"
            elif all_mark and not choice_mark and sep == "+":
                root = AllNode(children=tuple(sites),
                               rotation_weeks_per_month=rotation.days if rotation else None,
                               span=(0, len(head)), keyword_span=all_mark)
                kind = "all"
            else:
                if not choice_mark and not all_mark:
                    ambiguity.append("missing_choice_or_rotation_keyword")
                elif choice_mark and all_mark:
                    ambiguity.append("conflicting_keywords")
                else:
                    ambiguity.append("keyword_separator_mismatch")
                kind = "ambiguous"
    if rotation and kind != "all":
        notes.append("rotation_without_all")
    return LocationExpr(
        raw=text, kind=kind, root=root, constraints=tuple(constraints),
        sites=tuple(sites) if kind == "ambiguous" else (),
        ambiguous=bool(ambiguity), ambiguity_reasons=tuple(ambiguity),
        notes=tuple(notes), rules_version=rules_version, table_version=table.version,
    )


def _iter_sites(expr: LocationExpr) -> list[SiteNode]:
    root = expr.root
    if isinstance(root, SiteNode):
        return [root]
    if isinstance(root, (ChoiceNode, AllNode)):
        return list(root.options if isinstance(root, ChoiceNode) else root.children)
    return list(expr.sites)


def _site_label(site: SiteNode) -> str:
    return f"{site.city}·{site.district}" if site.district else site.city


def expr_cities(expr: LocationExpr) -> list[str]:
    """Distinct city labels in source order; district identity stays per city."""

    labels: list[str] = []
    for site in _iter_sites(expr):
        label = _site_label(site)
        if label not in labels:
            labels.append(label)
    if not labels and isinstance(expr.root, RemoteNode):
        labels.append("远程" + (f"（{expr.root.scope}）" if expr.root.scope else ""))
    return labels


def expr_summary(expr: LocationExpr) -> str:
    root = expr.root
    if isinstance(root, SiteNode):
        return _site_label(root)
    if isinstance(root, ChoiceNode):
        return "/".join(_site_label(o) for o in root.options) + "（任选一）"
    if isinstance(root, AllNode):
        return "+".join(_site_label(c) for c in root.children) + "（轮转）"
    if isinstance(root, RemoteNode):
        label = "远程" + (f"（{root.scope}）" if root.scope else "")
        visits = [c for c in expr.constraints if c.kind == "monthly_visit"]
        if visits:
            label += "；" + "；".join(f"每月赴{c.city}{c.days}天" for c in visits)
        return label
    if expr.kind == "ambiguous":
        return "/".join(_site_label(s) for s in expr.sites) + "（待解释）"
    return expr.raw
