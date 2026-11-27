from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from leads.pipeline.normalise import map_category
from leads.settings import load_categories
from leads.sources import osm
from leads.sources.osm import (
    OsmSource,
    age_date,
    category_of,
    index_extracts,
    read_extract,
    to_record,
)

SINCE = date(2018, 1, 1)
XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
 <node id="1" version="1" timestamp="2021-05-01T00:00:00Z" lat="35.9" lon="14.5">
  <tag k="name" v="Rosie's"/><tag k="shop" v="bakery"/><tag k="phone" v="+356 1; +356 2"/>
  <tag k="contact:email" v="a@b.mt"/><tag k="addr:housenumber" v="1"/><tag k="addr:street" v="Main"/>
 </node>
 <node id="2" version="4" timestamp="2024-05-01T00:00:00Z" lat="35.9" lon="14.5">
  <tag k="name" v="Bench"/><tag k="amenity" v="bench"/>
 </node>
 <node id="3" version="1" timestamp="2021-05-01T00:00:00Z" lat="35.0" lon="14.0"/>
 <node id="4" version="1" timestamp="2021-05-01T00:00:00Z" lat="36.0" lon="15.0"/>
 <way id="5" version="3" timestamp="2024-05-01T00:00:00Z">
  <nd ref="3"/><nd ref="4"/><tag k="name" v="Grand"/><tag k="tourism" v="hotel"/>
 </way>
</osm>
"""


def test_reading_an_extract(tmp_path: Path) -> None:
    path = tmp_path / "x.osm"
    path.write_text(XML, encoding="utf-8")
    shop, hotel = list(read_extract(path, SINCE))
    assert (shop.source_record_id, shop.category, shop.phone) == ("node/1", "shop=bakery", "+356 1")
    assert (shop.email, shop.address, shop.created_date) == ("a@b.mt", "1 Main", date(2021, 5, 1))
    assert (hotel.source_record_id, hotel.lon, hotel.lat) == ("way/5", 14.5, 35.5)
    assert hotel.created_date is None  # an edited place: first mapping date unknown


def test_age_date() -> None:
    assert age_date(1, date(2020, 1, 1), SINCE) == date(2020, 1, 1)
    assert age_date(3, date(2020, 1, 1), SINCE) is None
    assert age_date(3, date(2015, 1, 1), SINCE) == date(2015, 1, 1)


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"shop": "hairdresser"}, "shop=hairdresser"),
        ({"shop": "vacant"}, None),
        ({"amenity": "place_of_worship"}, None),
        ({"office": "government"}, None),
        ({"office": "lawyer"}, "office=lawyer"),
        ({"leisure": "park"}, None),
        ({"craft": "plumber", "office": "company"}, "craft=plumber"),
    ],
)
def test_category(tags: dict[str, str], expected: str | None) -> None:
    assert category_of(tags) == expected


def test_nameless_places_are_skipped() -> None:
    assert to_record("node", 1, 1, SINCE, {"shop": "yes"}, 1, 1, SINCE) is None


def test_groups() -> None:
    cfg = load_categories()
    assert map_category("osm", "shop=hairdresser", cfg) == "beauty_wellness"
    assert map_category("osm", "office=estate_agent", cfg) == "real_estate"
    assert map_category("osm", "shop=clothes", cfg) == "retail"


def test_only_wanted_countries_inside_their_box() -> None:
    class Fake:
        def countries(self, points: list[tuple[float, float]]) -> list[str | None]:
            return ["MT", "IL", None, "MT"][: len(points)]

    def record(lon: float) -> object:
        return to_record("node", 1, 1, SINCE, {"name": "A", "shop": "x"}, lon, 35.9, SINCE)

    source = OsmSource("x", {"MT": (14.1, 35.7, 14.7, 36.2)})
    batch = [record(14.5), record(14.5), record(14.5), record(20.0)]
    kept = list(source._located(batch, Fake(), {"MT"}))  # type: ignore[arg-type]
    assert [r.country_iso2 for r in kept] == ["MT"]


def test_extracts_come_from_the_geofabrik_index(tmp_path: Path) -> None:
    table = index_extracts(Path(__file__).parent / "fixtures" / "geofabrik_index.json")
    assert table["MT"] == ("europe/malta",)
    assert table["PS"] == table["IL"] == ("asia/israel-and-palestine",)
    assert table["AE"] == ("asia/gcc-states",)
    assert table["DO"] == table["HT"] == ("central-america/haiti-and-domrep",)
    assert table["US"] == ("north-america/us",)  # the country, not a state
    assert table["GB"] == ("europe/united-kingdom",)


def test_a_country_box_does_not_shrink_the_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "geofabrik_index.json"
    monkeypatch.setattr(osm, "download", lambda url, dest: fixture)
    table = OsmSource(tmp_path, {"GB": (-8.8, 49.8, 2.0, 61.0)}).extracts()
    assert table["GB"] == ("europe/united-kingdom",) and table["SA"] == ("asia/gcc-states",)
