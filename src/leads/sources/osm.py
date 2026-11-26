"""OpenStreetMap extracts (Geofabrik .osm.pbf).

Attribution: "© OpenStreetMap contributors", ODbL (see leads.export.ATTRIBUTION).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import UTC, date
from pathlib import Path

import duckdb
import osmium
import structlog

from leads.sources.base import RawRecord
from leads.sources.registries.download import download

log = structlog.get_logger(__name__)

BASE_URL = "https://download.geofabrik.de"
# Country outlines (public domain). Some extracts hold several countries.
BORDERS_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"

# Geofabrik's list of extracts, with the country codes each one covers.
INDEX_URL = f"{BASE_URL}/index-v1-nogeom.json"
_PBF = "-latest.osm.pbf"
# Extracts the index lists without a country code.
EXTRA_EXTRACTS: dict[str, tuple[str, ...]] = {
    "GG": ("europe/guernsey-jersey",), "JE": ("europe/guernsey-jersey",),
    "IM": ("europe/isle-of-man",), "XK": ("europe/kosovo",),
    "SA": ("asia/gcc-states",),  # in the GCC extract, but not among its codes
}  # fmt: skip
ENGLAND = "europe/united-kingdom/england"
# Leeds to Manchester, used while country_bbox keeps its GB test box.
GB_TEST = (
    f"{ENGLAND}/greater-manchester",
    f"{ENGLAND}/west-yorkshire",
    f"{ENGLAND}/south-yorkshire",
)


def index_extracts(path: Path) -> dict[str, tuple[str, ...]]:
    """``{ISO2: (region,)}`` from Geofabrik's index; a region is the URL path without suffix.

    A country listed in several extracts gets the widest one (fewest path parts), which is
    the whole country rather than one of its states.
    """
    found: dict[str, list[str]] = {}
    for feature in json.loads(path.read_text(encoding="utf-8")).get("features", []):
        props = feature.get("properties") or {}
        url = str((props.get("urls") or {}).get("pbf", ""))
        if not (url.startswith(BASE_URL + "/") and url.endswith(_PBF)):
            continue
        parts = url[len(BASE_URL) + 1 : -len(_PBF)].split("/")
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            parts.pop()  # europe/malta/malta is also served as europe/malta
        for code in props.get("iso3166-1:alpha2") or []:
            found.setdefault(str(code).upper(), []).append("/".join(parts))
    return {
        code: (min(regions, key=lambda r: (r.count("/"), len(r))),)
        for code, regions in found.items()
    }


AMENITIES = frozenset(
    {"restaurant", "cafe", "bar", "pub", "fast_food", "food_court", "ice_cream", "biergarten",
     "pharmacy", "clinic", "dentist", "doctors", "veterinary", "fuel", "car_rental", "car_wash",
     "driving_school", "language_school", "music_school", "kindergarten", "childcare",
     "nightclub", "cinema", "events_venue", "studio", "coworking_space", "internet_cafe",
     "animal_boarding", "training", "vehicle_inspection"}
)  # fmt: skip
LODGING = frozenset(
    {"hotel", "guest_house", "hostel", "motel", "apartment", "chalet", "camp_site",
     "caravan_site", "gallery"}
)  # fmt: skip
LEISURE = frozenset(
    {"fitness_centre", "sports_centre", "dance", "bowling_alley", "amusement_arcade",
     "escape_game", "golf_course", "horse_riding", "adult_gaming_centre"}
)  # fmt: skip
NOT_OFFICES = frozenset(
    {"government", "association", "ngo", "diplomatic", "political_party", "religion",
     "foundation", "charity", "union", "quango", "vacant"}
)  # fmt: skip
NOT_VALUES = frozenset({"no", "vacant", "yes;no"})


def category_of(tags: Mapping[str, str]) -> str | None:
    """``key=value`` of the tag that makes this a business, or None."""
    for key in ("shop", "craft"):
        if (value := tags.get(key)) and value not in NOT_VALUES:
            return f"{key}={value}"
    for key, allowed in (("amenity", AMENITIES), ("tourism", LODGING), ("leisure", LEISURE)):
        if (value := tags.get(key)) in allowed:
            return f"{key}={value}"
    if (value := tags.get("office")) and value not in NOT_OFFICES | NOT_VALUES:
        return f"office={value}"
    if (value := tags.get("healthcare")) and value not in NOT_VALUES | {"hospital"}:
        return f"healthcare={value}"
    return None


def age_date(version: int, changed: date, since: date) -> date | None:
    """What the extract says about when the place was first mapped.

    It holds only the latest version: for a first version that is the mapping date. A later
    version tells nothing, unless even the latest edit is older than the cut-off.
    """
    return changed if version == 1 or changed < since else None


def _first(tags: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        if value := tags.get(key, "").split(";")[0].strip():
            return value
    return None


def to_record(
    kind: str, osm_id: int, version: int, changed: date, tags: Mapping[str, str],
    lon: float, lat: float, since: date,
) -> RawRecord | None:  # fmt: skip
    category = category_of(tags)
    name = tags.get("name", "").strip()
    if category is None or not name:
        return None
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    socials = [
        url if "/" in url else f"https://www.{site}.com/{url.lstrip('@')}"
        for site in ("facebook", "instagram")
        if (url := _first(tags, f"contact:{site}", site))
    ]
    return RawRecord(
        source="osm",
        source_record_id=f"{kind}/{osm_id}",
        name=name,
        category=category,
        phone=_first(tags, "phone", "contact:phone", "mobile", "contact:mobile"),
        email=_first(tags, "email", "contact:email"),
        website=_first(tags, "website", "contact:website", "url"),
        address=street or None,
        city=_first(tags, "addr:city", "addr:suburb", "addr:town"),
        postcode=_first(tags, "addr:postcode"),
        lon=lon,
        lat=lat,
        created_date=age_date(version, changed, since),
        raw={"version": version, "changed": changed.isoformat(), "socials": socials,
             "brand": tags.get("brand")},
    )  # fmt: skip


def read_extract(path: Path, since: date) -> Iterator[RawRecord]:
    """Businesses of one extract; buildings are placed at the middle of their outline."""
    keys = osmium.filter.KeyFilter(
        "shop", "craft", "amenity", "tourism", "leisure", "office", "healthcare"
    )
    processor = (
        osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations()
        .with_filter(osmium.filter.EmptyTagFilter())
        .with_filter(osmium.filter.KeyFilter("name"))
        .with_filter(keys)
    )
    for item in processor:
        if isinstance(item, osmium.osm.Node):
            kind, lon, lat = "node", item.location.lon, item.location.lat
        elif isinstance(item, osmium.osm.Way):
            points = [(n.lon, n.lat) for n in item.nodes if n.location.valid()]
            if not points:
                continue
            kind = "way"
            lon = sum(p[0] for p in points) / len(points)
            lat = sum(p[1] for p in points) / len(points)
        else:
            continue
        record = to_record(
            kind, item.id, item.version, item.timestamp.astimezone(UTC).date(),
            {t.k: t.v for t in item.tags}, lon, lat, since,
        )  # fmt: skip
        if record:
            yield record


class Borders:
    """Which country a point lies in."""

    def __init__(self, path: Path) -> None:
        self._con = duckdb.connect()
        self._con.execute("INSTALL spatial; LOAD spatial")
        self._con.execute(
            "CREATE TABLE borders AS SELECT upper(ISO_A2_EH) AS iso, geom "
            f"FROM ST_Read('/vsizip/{str(path).replace(chr(39), chr(39) * 2)}')"
        )

    def countries(self, points: Sequence[tuple[float, float]]) -> list[str | None]:
        if not points:
            return []
        rows = self._con.execute(
            "WITH p AS (SELECT unnest(range(len($lon))) AS i, unnest($lon) AS lon, "
            "unnest($lat) AS lat) "
            "SELECT i, min(iso) FROM p JOIN borders ON ST_Contains(geom, ST_Point(lon, lat)) "
            "GROUP BY i",
            {"lon": [p[0] for p in points], "lat": [p[1] for p in points]},
        ).fetchall()
        found = {int(i): str(iso) for i, iso in rows}
        return [found.get(i) for i in range(len(points))]


class OsmSource:
    name = "osm"

    def __init__(
        self, extracts_dir: str | Path, bbox: Mapping[str, Sequence[float]] | None = None
    ) -> None:
        self.extracts_dir = Path(extracts_dir)
        self.bbox = bbox or {}

    def _inside(self, country: str, record: RawRecord) -> bool:
        box = self.bbox.get(country)
        if box is None or record.lon is None or record.lat is None:
            return True
        west, south, east, north = box
        return west <= record.lon <= east and south <= record.lat <= north

    def extracts(self) -> dict[str, tuple[str, ...]]:
        """Extract regions per country, from Geofabrik's index (refreshed monthly)."""
        index = download(INDEX_URL, self.extracts_dir / "geofabrik-index.json")
        table = {**index_extracts(index), **EXTRA_EXTRACTS}
        if "GB" in self.bbox:
            table["GB"] = GB_TEST
        return table

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Businesses in ``countries``; countries without an extract are skipped."""
        wanted = {c.upper() for c in countries}
        table = self.extracts()
        if unknown := sorted(wanted - table.keys()):
            log.warning("osm_no_extract", countries=unknown)
        regions = sorted({r for c in wanted & table.keys() for r in table[c]})
        if not regions:
            return
        borders = Borders(download(BORDERS_URL, self.extracts_dir / "borders.zip"))
        for region in regions:
            path = download(
                f"{BASE_URL}/{region}-latest.osm.pbf",
                self.extracts_dir / f"{region.replace('/', '_')}.osm.pbf",
            )
            batch: list[RawRecord] = []
            for record in read_extract(path, since):
                batch.append(record)
                if len(batch) >= 5000:
                    yield from self._located(batch, borders, wanted)
                    batch = []
            yield from self._located(batch, borders, wanted)

    def _located(
        self, batch: list[RawRecord], borders: Borders, wanted: Collection[str]
    ) -> Iterator[RawRecord]:
        found = borders.countries([(r.lon or 0.0, r.lat or 0.0) for r in batch])
        for record, country in zip(batch, found, strict=True):
            if country and country in wanted and self._inside(country, record):
                record.country_iso2 = country
                yield record
