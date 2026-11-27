"""Czech Register of Economic Subjects (RES, Czech Statistical Office).

Free open data, a full snapshot twice a month (https://opendata.csu.gov.cz/soubory/od/od_org03/
res_data.csv): every economic subject with its ID (IČO), legal form, CZ-NACE activity, date of
creation and address. No phone, email or website.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.sources.base import RawRecord
from leads.sources.registries.download import download

BULK_URL = "https://opendata.csu.gov.cz/soubory/od/od_org03/res_data.csv"
FETCH_BATCH = 10_000
# Legal form codes 1xx (entrepreneurs and companies) and 2xx (co-operatives and other
# business forms). 3xx public bodies, 7xx associations and foundations, 8xx churches and
# 9xx international bodies are not leads.
BUSINESS_FORM_PREFIXES = ("1", "2")


class ResSource:
    name = "res"
    country_iso2 = "CZ"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "res"

    def csv_file(self) -> Path:
        return download(BULK_URL, self.data_dir / "res_data.csv")

    def rows(self, path: Path) -> Iterator[dict[str, Any]]:
        prefixes = " OR ".join(f"starts_with(FORMA, '{p}')" for p in BUSINESS_FORM_PREFIXES)
        cursor = duckdb.connect().execute(
            f"""
            SELECT ICO AS number, FIRMA AS name, FORMA AS form, NACE AS nace,
                TRY_CAST(DDATVZN AS DATE) AS created,
                concat_ws(' ', NULLIF(ULICE_TEXT, ''),
                          concat_ws('/', NULLIF(CDOM, ''), NULLIF(COR, ''))) AS address,
                OBEC_TEXT AS city, PSC AS postcode
            FROM read_csv('{path}', header = true, all_varchar = true, quote = '"',
                          escape = '"')
            WHERE (DDATZAN IS NULL OR DDATZAN = '') AND ({prefixes})
            """
        )
        columns = [d[0] for d in cursor.description or []]
        while batch := cursor.fetchmany(FETCH_BATCH):
            for values in batch:
                yield dict(zip(columns, values, strict=True))

    def to_record(self, row: Mapping[str, Any]) -> RawRecord:
        return RawRecord(
            source=self.name,
            source_record_id=str(row["number"]),
            name=str(row["name"]),
            country_iso2=self.country_iso2,
            category=row["nace"] or None,  # a CZ-NACE code
            address=row["address"] or None,
            city=row["city"] or None,
            postcode=row["postcode"] or None,
            created_date=row["created"],
            shared_ids=[f"res:{row['number']}"],
            raw={"form": row["form"]},
        )

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active subjects of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for row in self.rows(self.csv_file()):
            if row["name"] and row["number"]:
                yield self.to_record(row)
