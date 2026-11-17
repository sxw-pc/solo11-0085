"""Offline synthetic city/alias tables for location expressions.

All names and codes are synthetic and versioned; no geocoding is ever
performed. ``TABLE_V1`` is kept so rule-version upgrades can be replayed
and audited against the previous parse of the same raw text.
"""

from __future__ import annotations

from dataclasses import dataclass


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


def _city(code: str, name: str, aliases: tuple[str, ...],
          districts: tuple[str, ...]) -> CityEntry:
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
