"""US Secretaries of State that publish their business register as a free bulk file.

New York (Department of State, active corporations) and Colorado (Secretary of State,
business entities), both on Socrata open data portals. Neither lists a phone, email or
website. Their CSV exports write dates as month/day/year.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.sources.base import RawRecord
from leads.sources.registries.download import download

FETCH_BATCH = 10_000
US_DATE = "%m/%d/%Y"


class _StateSource:
    """One state's register: a CSV export narrowed to active business entities by ``query``."""

    name = ""
    state = ""
    url = ""
    country_iso2 = "US"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / self.name

    def csv_file(self) -> Path:
        return download(self.url, self.data_dir / "entities.csv")

    def query(self, scan: str) -> str:
        raise NotImplementedError

    def rows(self, path: Path) -> Iterator[dict[str, Any]]:
        scan = f"read_csv('{path}', header = true, all_varchar = true, quote = '\"', escape = '\"')"
        cursor = duckdb.connect().execute(self.query(scan))
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
            category=row["kind"] or None,
            address=row["address"] or None,
            city=str(row["city"]).title() if row["city"] else None,
            region=row["region"] or self.state,
            postcode=row["postcode"] or None,
            created_date=row["created"],
            shared_ids=[f"{self.name}:{row['number']}"],
        )

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active entities of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for row in self.rows(self.csv_file()):
            if row["name"] and row["number"]:
                yield self.to_record(row)


class NewYorkSource(_StateSource):
    name = "us_ny"
    state = "NY"
    url = "https://data.ny.gov/api/views/n9v6-gdp6/rows.csv?accessType=DOWNLOAD"

    def query(self, scan: str) -> str:
        # The business location when given, else the address for service of process.
        return f"""
            SELECT "DOS ID" AS number, "Current Entity Name" AS name, "Entity Type" AS kind,
                TRY_STRPTIME("Initial DOS Filing Date", '{US_DATE}')::DATE AS created,
                CASE WHEN NULLIF("Location Address 1", '') IS NOT NULL
                     THEN "Location Address 1" ELSE "DOS Process Address 1" END AS address,
                CASE WHEN NULLIF("Location Address 1", '') IS NOT NULL
                     THEN "Location City" ELSE "DOS Process City" END AS city,
                CASE WHEN NULLIF("Location Address 1", '') IS NOT NULL
                     THEN "Location State" ELSE "DOS Process State" END AS region,
                CASE WHEN NULLIF("Location Address 1", '') IS NOT NULL
                     THEN "Location Zip" ELSE "DOS Process Zip" END AS postcode
            FROM {scan}
            WHERE "Entity Type" NOT ILIKE '%NOT-FOR-PROFIT%'
        """


class ColoradoSource(_StateSource):
    name = "us_co"
    state = "CO"
    url = "https://data.colorado.gov/api/views/4ykn-tg5h/rows.csv?accessType=DOWNLOAD"
    # Dissolved, withdrawn, revoked, merged and delinquent entities are left out.
    ACTIVE = ("Good Standing", "Exists")

    def query(self, scan: str) -> str:
        active = ", ".join(f"'{s}'" for s in self.ACTIVE)
        # DNC and FNC are domestic and foreign non-profit corporations.
        return f"""
            SELECT entityid AS number, entityname AS name, entitytype AS kind,
                TRY_STRPTIME(entityformdate, '{US_DATE}')::DATE AS created,
                concat_ws(' ', NULLIF(principaladdress1, ''), NULLIF(principaladdress2, ''))
                    AS address,
                principalcity AS city, principalstate AS region, principalzipcode AS postcode
            FROM {scan}
            WHERE entitystatus IN ({active}) AND entitytype NOT IN ('DNC', 'FNC')
              AND COALESCE(NULLIF(principalcountry, ''), 'US') = 'US'
        """
