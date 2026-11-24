"""Deduplication / matching of normalised source records into one lead per business.

Match keys, strongest first:
1. shared ids (e.g. a registry number present in two sources)
2. domain (same country)
3. phone (E.164)
4. normalised name + postcode (same country)
5. similar name within ``geo_radius_m`` (default 50 m)

A domain or phone shared by more than ``max_shared_key_group`` records is treated as a
chain/franchise/call-centre and NOT used as a key.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

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
) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, rec in enumerate(records):
        for key in key_fn(rec):
            groups[key].append(i)
    for members in groups.values():
        if len(members) < 2 or (max_group is not None and len(members) > max_group):
            continue
        for other in members[1:]:
            uf.union(members[0], other)


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
    radius_m: float = 50.0,
    max_shared_key_group: int = 8,
) -> list[list[int]]:
    """Group record indices that refer to the same business. Each group becomes one lead."""
    uf = _UnionFind(len(records))
    _union_by_key(uf, records, lambda r: r.shared_ids, None)
    _union_by_key(
        uf,
        records,
        lambda r: [f"{r.country_iso2}|{r.domain}"] if r.domain else [],
        max_shared_key_group,
    )
    _union_by_key(
        uf, records, lambda r: [r.phone_e164] if r.phone_e164 else [], max_shared_key_group
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
    """lead_id for a NEW cluster.

    TODO(build step 5): before calling this, look up ``lead_sources`` for any record of
    the cluster; if one already belongs to a lead, reuse that lead_id so ids stay stable
    across monthly runs (and merge leads if the cluster spans several existing ids).
    """
    rep = representative(records)
    return make_lead_id(rep.source, rep.source_record_id)


def merge_fields(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    """Merge normalised rows of one cluster into a single lead row.

    Rule: for each field take the first non-empty value in SOURCE_PRIORITY order;
    ``sources`` = sorted distinct source names; ``created_date`` is resolved separately
    by ``pipeline.age``.

    TODO(build step 5): implement + tests (prefer registry name/address, FSQ phone, etc.).
    """
    raise NotImplementedError("match.merge_fields (build step 5)")
