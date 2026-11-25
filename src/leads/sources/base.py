"""Source protocol, the RawRecord shape and the raw zone writer.

Raw zone layout: ``data/raw/source=<name>/run=<run_id>/part-00000.parquet``
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import polars as pl


@dataclass(slots=True)
class RawRecord:
    """One business as delivered by a source, before normalisation."""

    source: str
    source_record_id: str
    name: str
    country_iso2: str | None = None
    category: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    created_date: date | None = None  # registry incorporation / fsq date_created, if any
    closed_date: date | None = None
    shared_ids: list[str] = field(default_factory=list)  # e.g. ["companies_house:01234567"]
    raw: dict[str, Any] = field(default_factory=dict)  # original fields, stored as JSON


RAW_SCHEMA: dict[str, Any] = {
    "source": pl.String,
    "source_record_id": pl.String,
    "name": pl.String,
    "country_iso2": pl.String,
    "category": pl.String,
    "phone": pl.String,
    "email": pl.String,
    "website": pl.String,
    "address": pl.String,
    "city": pl.String,
    "region": pl.String,
    "postcode": pl.String,
    "lat": pl.Float64,
    "lon": pl.Float64,
    "created_date": pl.Date,
    "closed_date": pl.Date,
    "shared_ids": pl.List(pl.String),
    "raw_json": pl.String,
}


@runtime_checkable
class Source(Protocol):
    """A data source. ``fetch`` yields records for ``countries``.

    ``since`` lets sources that know creation dates skip older businesses early, but a
    source must still yield records WITHOUT a date (their age is resolved later).
    """

    name: str

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]: ...


def _row(record: RawRecord) -> dict[str, Any]:
    row = asdict(record)
    row["raw_json"] = json.dumps(row.pop("raw"), default=str, ensure_ascii=False)
    return row


def raw_dir(data_dir: Path, source: str, run_id: str) -> Path:
    return data_dir / "raw" / f"source={source}" / f"run={run_id}"


def write_raw(
    records: Iterable[RawRecord],
    *,
    source: str,
    run_id: str,
    data_dir: Path,
    chunk_size: int = 100_000,
) -> tuple[int, list[Path]]:
    """Write records to the raw zone as parquet parts. Returns (row count, files)."""
    out_dir = raw_dir(data_dir, source, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    buffer: list[dict[str, Any]] = []
    total = 0

    def flush() -> None:
        if not buffer:
            return
        path = out_dir / f"part-{len(files):05d}.parquet"
        pl.DataFrame(buffer, schema=RAW_SCHEMA).write_parquet(path)
        files.append(path)
        buffer.clear()

    for record in records:
        buffer.append(_row(record))
        total += 1
        if len(buffer) >= chunk_size:
            flush()
    flush()
    return total, files


def country_batches(
    boxes: Mapping[str, Sequence[float]], countries: Collection[str]
) -> list[list[str]]:
    """Countries to ask for together: one by one where each has a box, else all at once."""
    ordered = sorted(countries)
    if ordered and all(c in boxes for c in ordered):
        return [[c] for c in ordered]
    return [ordered]


def bbox_sql(
    boxes: Mapping[str, Sequence[float]], countries: Collection[str], lon: str, lat: str
) -> str:
    """``AND (...)`` limiting a scan to the countries' [west, south, east, north] boxes.

    Files are ordered by location, so this skips most of the planet. Empty unless every
    requested country has a box. Rows without coordinates are kept.
    """
    if not countries or any(c not in boxes for c in countries):
        return ""
    parts = []
    for country in sorted(countries):
        west, south, east, north = (float(x) for x in boxes[country])
        parts.append(f"({lon} BETWEEN {west} AND {east} AND {lat} BETWEEN {south} AND {north})")
    return f"AND ({' OR '.join(parts)} OR {lat} IS NULL)"
