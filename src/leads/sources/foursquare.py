"""Foursquare Open Source Places (parquet, read with DuckDB).

Docs: https://docs.foursquare.com/data-products/docs/access-fsq-os-places
Provides ``date_created`` / ``date_refreshed`` / ``date_closed`` — useful age evidence.
Licence: Apache 2.0, attribution in exports.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord

FETCH_BATCH = 10_000


class FoursquareSource:
    name = "foursquare"

    def __init__(self, uri: str | None) -> None:
        self.uri = uri

    def build_query(self) -> str:
        """TODO(build step 2): verify column names for the downloaded release."""
        if not self.uri:
            raise NotImplementedError("Set sources.foursquare.uri in config/settings.yaml")
        return f"""
            SELECT
                fsq_place_id, name, latitude, longitude, address, locality, region, postcode,
                country, date_created, date_refreshed, date_closed, tel, website, email,
                facebook_id, instagram, twitter,
                array_to_string(fsq_category_labels, ' | ') AS category
            FROM read_parquet('{self.uri}')
            WHERE list_contains($countries, country)
              AND date_closed IS NULL
        """

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Stream open places for ``countries``. Rows are NOT dropped by ``since`` here:
        ``date_created`` is when the place entered Foursquare, which the age resolver
        combines with other evidence.

        TODO(build step 2): S3 access for FSQ OS Places may need credentials / Hugging Face
        download — document the chosen route in docs/runbook.md.
        """
        con = duckdb.connect()
        if self.uri and self.uri.startswith("s3://"):
            con.execute("INSTALL httpfs; LOAD httpfs;")
        cursor = con.execute(self.build_query(), {"countries": sorted(countries)})
        columns = [d[0] for d in cursor.description or []]
        while rows := cursor.fetchmany(FETCH_BATCH):
            for values in rows:
                row = dict(zip(columns, values, strict=True))
                yield RawRecord(
                    source=self.name,
                    source_record_id=str(row["fsq_place_id"]),
                    name=row["name"],
                    country_iso2=row["country"],
                    category=row["category"],
                    phone=row["tel"],
                    email=row["email"],
                    website=row["website"],
                    address=row["address"],
                    city=row["locality"],
                    region=row["region"],
                    postcode=row["postcode"],
                    lat=row["latitude"],
                    lon=row["longitude"],
                    created_date=parse_date_loose(row["date_created"]),
                    raw={
                        "date_refreshed": row["date_refreshed"],
                        "facebook_id": row["facebook_id"],
                        "instagram": row["instagram"],
                        "twitter": row["twitter"],
                    },
                )
