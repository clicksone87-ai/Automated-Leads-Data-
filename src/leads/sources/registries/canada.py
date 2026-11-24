"""Corporations Canada: federal business corporations (open data CSV).

Provincial registries (Ontario, Quebec, BC) are separate and not covered.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord
from leads.sources.registries.download import download

BULK_URL = "https://d4bf66bykfyaf.cloudfront.net/corporations-active-cbca-en.csv"
FETCH_BATCH = 10_000


class CanadaSource:
    name = "canada"
    country_iso2 = "CA"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "canada"

    def csv_file(self) -> Path:
        return download(BULK_URL, self.data_dir / "corporations-active-cbca-en.csv")

    def rows(self, path: Path) -> Iterator[dict[str, Any]]:
        cursor = duckdb.connect().execute(
            f"""
            SELECT "Corporation number" AS number, "Corporate name - form 1" AS name,
                "Corporate name - form 2" AS name2, "Anniversary date" AS incorporated,
                "Street" AS address, "City/town" AS city, "Province/territory" AS region,
                "Postal code" AS postcode
            FROM read_csv('{path}', header = true, all_varchar = true, quote = '"', escape = '"')
            WHERE "Status" = 'Active' AND "Country" = 'CA'
            """
        )
        columns = [d[0] for d in cursor.description or []]
        while batch := cursor.fetchmany(FETCH_BATCH):
            for values in batch:
                yield dict(zip(columns, values, strict=True))

    def to_records(self, row: Mapping[str, Any]) -> Iterator[RawRecord]:
        """One record per language form of the name (French and English names differ)."""
        for suffix, name in (("", row["name"]), (":2", row["name2"])):
            if not name or (suffix and name == row["name"]):
                continue
            yield RawRecord(
                source=self.name,
                source_record_id=f"{row['number']}{suffix}",
                name=name,
                country_iso2=self.country_iso2,
                address=row["address"],
                city=row["city"],
                region=row["region"],
                postcode=row["postcode"],
                created_date=parse_date_loose(row["incorporated"]),
                shared_ids=[f"canada:{row['number']}"],
            )

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active corporations of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for row in self.rows(self.csv_file()):
            yield from self.to_records(row)
