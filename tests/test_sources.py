from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from leads.sources import foursquare
from leads.sources.base import bbox_sql, country_batches
from leads.sources.foursquare import COLUMNS, FoursquareSource, social_urls
from leads.sources.overture import OvertureSource, foursquare_ids, parse_releases

LISTING = """<?xml version="1.0"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<Prefix>release/</Prefix>
<CommonPrefixes><Prefix>release/2026-11-18.0/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>release/2026-10-21.0/</Prefix></CommonPrefixes>
</ListBucketResult>"""

BOXES = {"MT": [14.1, 35.7, 14.7, 36.2]}


def test_parse_releases_sorted_and_ignores_root() -> None:
    assert parse_releases(LISTING) == ["2026-10-21.0", "2026-11-18.0"]


def test_bbox_only_when_every_country_has_a_box() -> None:
    assert "14.1" in bbox_sql(BOXES, {"MT"}, "x", "y")
    assert bbox_sql(BOXES, {"MT", "FR"}, "x", "y") == ""
    assert bbox_sql(BOXES, set(), "x", "y") == ""


def test_foursquare_ids_from_overture_sources() -> None:
    sources = [
        {"dataset": "meta", "record_id": "1"},
        {"dataset": "Foursquare", "record_id": "abc"},
    ]
    assert foursquare_ids(sources) == ["foursquare:abc"]
    assert foursquare_ids(None) == []


def _overture_release(tmp_path: Path) -> OvertureSource:
    out = tmp_path / "rel" / "theme=places" / "type=place"
    out.mkdir(parents=True)
    place = """SELECT '{id}' AS id, {{'primary': {name}}} AS names,
        {{'primary': 'bakery', 'hierarchy': ['food', 'bakery']}} AS taxonomy, 'shop' AS basic_category,
        ['https://a.example'] AS websites, ['+35621234567'] AS phones,
        CAST([] AS VARCHAR[]) AS emails, ['https://facebook.com/a'] AS socials,
        [{{'freeform': 'x', 'locality': 'Valletta', 'region': NULL::VARCHAR,
           'postcode': 'VLT1', 'country': '{country}'}}] AS addresses,
        {{'xmin': 14.5, 'ymin': 35.9}} AS bbox, 0.9 AS confidence,
        {status}::VARCHAR AS operating_status,
        [{{'dataset': 'Foursquare', 'record_id': 'f{id}'}}] AS sources"""
    rows = [
        place.format(id="1", name="'A'", country="MT", status="'open'"),
        place.format(id="2", name="'B'", country="MT", status="'permanently_closed'"),
        place.format(id="3", name="'C'", country="JP", status="'open'"),
        place.format(id="4", name="'D'", country="MT", status="NULL"),
    ]
    duckdb.sql(f"COPY ({' UNION ALL '.join(rows)}) TO '{out}/p.parquet' (FORMAT parquet)")
    return OvertureSource(release="rel", base_uri=str(tmp_path), bbox=BOXES)


def test_overture_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _overture_release(tmp_path)
    monkeypatch.setattr(src, "connect", duckdb.connect)
    records = {r.source_record_id: r for r in src.fetch({"MT"}, date(2018, 1, 1))}
    assert set(records) == {"1", "4"}
    assert records["1"].category == "bakery"
    assert records["1"].raw["category_path"] == ["food", "bakery"]
    assert records["1"].email is None
    assert records["1"].shared_ids == ["foursquare:f1"]
    assert records["1"].raw["socials"] == ["https://facebook.com/a"]


def _foursquare_file(tmp_path: Path, drop: str | None = None) -> str:
    values = {c: "NULL::VARCHAR" for c in COLUMNS} | {
        "fsq_place_id": "'f1'", "name": "'A'", "country": "'MT'", "latitude": "35.9",
        "longitude": "14.5", "date_created": "'2019-05-01'", "facebook_id": "123",
        "fsq_category_labels": "['Retail', 'Shop']", "unresolved_flags": "CAST(NULL AS VARCHAR[])",
    }  # fmt: skip
    if drop:
        del values[drop]
    row = "SELECT " + ", ".join(f"{v} AS {c}" for c, v in values.items())
    closed = f"SELECT * REPLACE ('f2' AS fsq_place_id, '2024-01-01' AS date_closed) FROM ({row})"
    path = tmp_path / "fsq.parquet"
    flagged = (
        f"SELECT * REPLACE ('f3' AS fsq_place_id, ['duplicate'] AS unresolved_flags) FROM ({row})"
    )
    sql = row if drop else f"{row} UNION ALL {closed} UNION ALL {flagged}"
    duckdb.sql(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")
    return str(path)


def test_foursquare_fetch_skips_closed_and_flagged(tmp_path: Path) -> None:
    src = FoursquareSource(_foursquare_file(tmp_path))
    (record,) = src.fetch({"MT"}, date(2018, 1, 1))
    assert record.created_date == date(2019, 5, 1)
    assert record.category == "Retail | Shop"
    assert record.shared_ids == ["foursquare:f1"]
    assert record.raw["socials"] == ["https://www.facebook.com/123"]


def test_foursquare_missing_column_is_named(tmp_path: Path) -> None:
    src = FoursquareSource(_foursquare_file(tmp_path, drop="tel"))
    with pytest.raises(ValueError, match="tel"):
        list(src.fetch({"MT"}, date(2018, 1, 1)))


def test_foursquare_needs_token_or_uri() -> None:
    with pytest.raises(ValueError, match="HF_TOKEN"):
        FoursquareSource(None).build_query()


def test_social_urls() -> None:
    assert social_urls(None, "@shop", None) == ["https://www.instagram.com/shop"]


def test_boxed_countries_are_asked_one_by_one() -> None:
    boxes = {"AE": (1, 2, 3, 4), "SA": (1, 2, 3, 4)}
    assert country_batches(boxes, {"SA", "AE"}) == [["AE"], ["SA"]]
    assert country_batches(boxes, {"SA", "FR"}) == [["FR", "SA"]]


def test_release_is_known_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FoursquareSource(None, hf_token="t")
    monkeypatch.setattr(foursquare, "latest_release_uri", lambda token: "hf://found/*.parquet")
    seen: list[str | None] = []

    def connect() -> None:
        seen.append(source.uri)
        raise RuntimeError("stop")

    monkeypatch.setattr(source, "connect", connect)
    with pytest.raises(RuntimeError, match="stop"):
        list(source.fetch({"MT"}, date(2018, 1, 1)))
    assert seen == ["hf://found/*.parquet"]
