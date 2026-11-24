"""UK Companies House: the free monthly company data product (CSV, no key needed).

https://download.companieshouse.gov.uk/en_output.html
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord
from leads.sources.registries.download import download, get_text

BASE_URL = "https://download.companieshouse.gov.uk/"
FETCH_BATCH = 10_000
COLUMNS = {
    "CompanyName": "name",
    "CompanyNumber": "number",
    "RegAddress.AddressLine1": "address",
    "RegAddress.PostTown": "city",
    "RegAddress.County": "region",
    "RegAddress.PostCode": "postcode",
    "IncorporationDate": "incorporated",
    "SICCode.SicText_1": "activity",
}


def bulk_file_name(page_html: str) -> str:
    """Name of the one-file product on the download page."""
    names = re.findall(r"BasicCompanyDataAsOneFile-\d{4}-\d{2}-\d{2}\.zip", page_html)
    if not names:
        raise RuntimeError("Companies House bulk file not found on the download page")
    return max(names)


class CompaniesHouseSource:
    name = "companies_house"
    country_iso2 = "GB"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "companies_house"

    def csv_file(self) -> Path:
        archive_name = bulk_file_name(get_text(BASE_URL + "en_output.html"))
        archive = download(BASE_URL + archive_name, self.data_dir / archive_name)
        target = archive.with_suffix(".csv")
        if not target.exists():
            with zipfile.ZipFile(archive) as zf:
                member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
                part = target.with_name(target.name + ".part")
                with zf.open(member) as src, part.open("wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                part.replace(target)
        return target

    def rows(self, path: Path) -> Iterator[dict[str, Any]]:
        con = duckdb.connect()
        scan = f"read_csv('{path}', header = true, all_varchar = true, quote = '\"', escape = '\"')"
        # Some header cells have a leading space.
        actual = {
            r[0].strip(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {scan}").fetchall()
        }
        missing = sorted({*COLUMNS, "CompanyStatus"} - set(actual))
        if missing:
            raise ValueError(f"Companies House file is missing columns: {', '.join(missing)}")
        select = ", ".join(f'"{actual[c]}" AS {alias}' for c, alias in COLUMNS.items())
        cursor = con.execute(
            f"SELECT {select} FROM {scan} "
            f"WHERE starts_with(lower(\"{actual['CompanyStatus']}\"), 'active')"
        )
        names = list(COLUMNS.values())
        while batch := cursor.fetchmany(FETCH_BATCH):
            for values in batch:
                yield dict(zip(names, values, strict=True))

    def to_record(self, row: Mapping[str, Any]) -> RawRecord:
        return RawRecord(
            source=self.name,
            source_record_id=row["number"],
            name=row["name"],
            country_iso2=self.country_iso2,
            category=row["activity"],
            address=row["address"],
            city=row["city"],
            region=row["region"],
            postcode=row["postcode"],
            created_date=parse_date_loose(row["incorporated"]),
            shared_ids=[f"companies_house:{row['number']}"],
        )

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for row in self.rows(self.csv_file()):
            if row["name"] and row["number"]:
                yield self.to_record(row)
