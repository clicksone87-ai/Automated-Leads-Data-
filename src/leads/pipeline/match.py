"""Deduplication / matching of normalised source records into one lead per business.

Match keys, strongest first:
1. shared ids (e.g. a registry number present in two sources)
2. domain (same country), when the names are alike
3. phone (E.164), when the names are alike
4. normalised name + postcode (same country)
5. similar name within ``geo_radius_m`` (default 50 m)

A domain or phone shared by more than ``max_shared_key_group`` records is treated as a
chain/franchise/call-centre and NOT used as a key. Names must agree because one website or
phone often serves several businesses (a hotel and its bar, an agency and its clients).
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from leads.pipeline.normalise import is_social_domain

# Lower number = more trusted when picking the representative record / field values.
SOURCE_PRIORITY: dict[str, int] = {
    "companies_house": 0,
    "sirene": 0,
    "brreg": 0,
    "abn": 0,
    "canada": 0,
    "foursquare": 1,
    "overture": 2,
    "osm": 3,
    "directories": 4,
}


@dataclass(slots=True)
class MatchRecord:
    source: str
    source_record_id: str
    name_norm: str
    country_iso2: str | None = None
    domain: str | None = None
    phone_e164: str | None = None
    postcode_key: str | None = None
    lat: float | None = None
    lon: float | None = None
    shared_ids: tuple[str, ...] = ()


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# A word in at least this many names, and this share of them, says little about identity.
_COMMON_WORD_MIN = 20
_COMMON_WORD_SHARE = 0.002


def distinctive_names(names: Sequence[str]) -> list[str]:
    """Names without words common in the data ("malta", "hotel", "centre").

    A name made only of common words is kept whole.
    """
    seen: Counter[str] = Counter(w for name in names for w in set(name.split()))
    limit = max(_COMMON_WORD_MIN, _COMMON_WORD_SHARE * len(names))
    result = []
    for name in names:
        rare = [w for w in name.split() if seen[w] < limit]
        result.append(" ".join(rare) or name)
    return result


def name_likeness(a: str, b: str) -> float:
    """0-100. Extra words on one side and different word spacing are tolerated."""
    joined = fuzz.ratio(a.replace(" ", ""), b.replace(" ", ""))
    return max(fuzz.token_set_ratio(a, b), joined)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _union_by_key(
    uf: _UnionFind,
    records: Sequence[MatchRecord],
    key_fn: Callable[[MatchRecord], Iterable[str]],
    max_group: int | None,
    alike: Callable[[int, int], bool] | None = None,
) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, rec in enumerate(records):
        for key in key_fn(rec):
            groups[key].append(i)
    for members in groups.values():
        if len(members) < 2 or (max_group is not None and len(members) > max_group):
            continue
        if alike is None:
            for other in members[1:]:
                uf.union(members[0], other)
            continue
        for n, i in enumerate(members):
            for j in members[n + 1 :]:
                if alike(i, j):
                    uf.union(i, j)


def _union_by_geo(
    uf: _UnionFind, records: Sequence[MatchRecord], radius_m: float, name_threshold: int
) -> None:
    cell_deg = radius_m / 111_320.0
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, rec in enumerate(records):
        if rec.lat is not None and rec.lon is not None and rec.name_norm:
            cells[(math.floor(rec.lat / cell_deg), math.floor(rec.lon / cell_deg))].append(i)
    for (cy, cx), members in cells.items():
        lat = (cy + 0.5) * cell_deg
        # Longitude cells shrink towards the poles: widen the neighbour search accordingly.
        span = min(50, math.ceil(1 / max(math.cos(math.radians(lat)), 1e-6)))
        for dy in (-1, 0, 1):
            for dx in range(-span, span + 1):
                for j in cells.get((cy + dy, cx + dx), ()):
                    for i in members:
                        if i >= j:
                            continue
                        a, b = records[i], records[j]
                        assert a.lat is not None and a.lon is not None
                        assert b.lat is not None and b.lon is not None
                        if a.country_iso2 != b.country_iso2:
                            continue
                        if haversine_m(a.lat, a.lon, b.lat, b.lon) > radius_m:
                            continue
                        if fuzz.token_sort_ratio(a.name_norm, b.name_norm) >= name_threshold:
                            uf.union(i, j)


def cluster_records(
    records: Sequence[MatchRecord],
    *,
    name_threshold: int = 90,
    shared_key_threshold: int = 75,
    radius_m: float = 50.0,
    max_shared_key_group: int = 8,
) -> list[list[int]]:
    """Group record indices that refer to the same business. Each group becomes one lead."""
    uf = _UnionFind(len(records))
    names = distinctive_names([r.name_norm for r in records])

    def alike(i: int, j: int) -> bool:
        return name_likeness(names[i], names[j]) >= shared_key_threshold

    _union_by_key(uf, records, lambda r: r.shared_ids, None)
    _union_by_key(
        uf,
        records,
        lambda r: [f"{r.country_iso2}|{r.domain}"] if r.domain else [],
        max_shared_key_group,
        alike,
    )
    _union_by_key(
        uf,
        records,
        lambda r: [r.phone_e164] if r.phone_e164 else [],
        max_shared_key_group,
        alike,
    )
    _union_by_key(
        uf,
        records,
        lambda r: (
            [f"{r.country_iso2}|{r.postcode_key}|{r.name_norm}"]
            if r.postcode_key and r.name_norm
            else []
        ),
        max_shared_key_group,
    )
    _union_by_geo(uf, records, radius_m, name_threshold)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(records)):
        clusters[uf.find(i)].append(i)
    return sorted(clusters.values())


def make_lead_id(source: str, source_record_id: str) -> str:
    """Stable 20-hex-char id derived from a source record."""
    return hashlib.sha1(f"{source}:{source_record_id}".encode()).hexdigest()[:20]


def representative(records: Sequence[MatchRecord]) -> MatchRecord:
    """Most trusted record of a cluster (registry > Foursquare > Overture > OSM > directories)."""
    return min(
        records, key=lambda r: (SOURCE_PRIORITY.get(r.source, 9), r.source, r.source_record_id)
    )


def lead_id_for_cluster(records: Sequence[MatchRecord]) -> str:
    """lead_id for a NEW cluster (``pipeline.process`` reuses the ids of known records)."""
    rep = representative(records)
    return make_lead_id(rep.source, rep.source_record_id)


# Filled field by field from the most trusted record that has a value.
_MERGED_FIELDS = ("name", "email", "country_iso2", "address", "city", "region", "postcode")
# Values that only make sense together, taken from one record.
_MERGED_GROUPS: tuple[tuple[str, ...], ...] = (
    ("phone_e164", "phone_type"),
    ("website", "domain"),
    ("lat", "lon"),
)


def sort_by_priority(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (SOURCE_PRIORITY.get(r["source"], 9), r["source"], r["source_record_id"]),
    )


def merge_fields(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merge normalised rows of one cluster into a single lead row.

    Each field takes the first non-empty value in SOURCE_PRIORITY order; ``created_date``
    is resolved separately by ``pipeline.age``.
    """
    ordered = sort_by_priority(rows)
    merged: dict[str, Any] = {
        field: next((r[field] for r in ordered if r.get(field)), None) for field in _MERGED_FIELDS
    }
    for group in _MERGED_GROUPS:
        donor = next((r for r in ordered if r.get(group[0]) is not None), None)
        for field in group:
            merged[field] = donor[field] if donor else None
    # A business's own site beats a social profile, whichever source listed it.
    own_site = next(
        (r for r in ordered if r.get("website") and not is_social_domain(r["domain"])), None
    )
    if own_site:
        merged["website"], merged["domain"] = own_site["website"], own_site["domain"]
    # A specific category beats "other".
    named = next((r for r in ordered if r["category"] and r["category_group"] != "other"), None)
    named = named or next((r for r in ordered if r["category"]), ordered[0])
    merged["category"], merged["category_group"] = named["category"], named["category_group"]
    merged["sources"] = sorted({r["source"] for r in rows})
    return merged
