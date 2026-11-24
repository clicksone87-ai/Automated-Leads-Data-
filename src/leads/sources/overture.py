"""Overture Maps Places (GeoParquet on S3, read with DuckDB).

Docs: https://docs.overturemaps.org/guides/places/ — no creation dates in Overture, so
age comes from other sources. Attribution required (see leads.export.ATTRIBUTION).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date

import duckdb

from leads.sources.base import RawRecord

FETCH_BATCH = 10_000


class OvertureSource:
    name = "overture"

    def __init__(
        self, release: str | None, base_uri: str = "s3://overturemaps-us-west-2/release"
    ) -> None:
        self.release = release
        self.base_uri = base_uri.rstrip("/")

    def places_uri(self) -> str:
        if not self.release:
            # TODO(build step 2): discover the latest release (list s3://.../release/) instead.
            raise NotImplementedError("Set sources.overture.release in config/settings.yaml")
        return f"{self.base_uri}/{self.release}/theme=places/type=place/*"

    def build_query(self) -> str:
        """SQL selecting the fields we need; country list is bound as parameter ``$countries``.

        TODO(build step 2): verify column names against the configured release — newer
        releases move from ``categories.primary`` to ``basic_category`` and add
        ``operating_status`` (filter out closed places when present).
        """
        return f"""
            SELECT
                id,
                names.primary                    AS name,
                categories.primary               AS category,
                websites[1]                      AS website,
                phones[1]                        AS phone,
                emails[1]                        AS email,
                addresses[1].freeform            AS address,
                addresses[1].locality            AS city,
                addresses[1].region              AS region,
                addresses[1].postcode            AS postcode,
                addresses[1].country             AS country_iso2,
                bbox.xmin                        AS lon,
                bbox.ymin                        AS lat,
                confidence
            FROM read_parquet('{self.places_uri()}', hive_partitioning = 1)
            WHERE list_contains($countries, addresses[1].country)
              AND names.primary IS NOT NULL
        """

    def connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region = 'us-west-2';")
        return con

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Stream places for ``countries``. ``since`` is unused (Overture has no dates).

        TODO(build step 2): run against 2-3 test countries first (e.g. GB, IE, MT);
        consider ``COPY (...) TO 'raw.parquet'`` for full-scale runs instead of Python rows.
        """
        con = self.connect()
        cursor = con.execute(self.build_query(), {"countries": sorted(countries)})
        columns = [d[0] for d in cursor.description or []]
        while rows := cursor.fetchmany(FETCH_BATCH):
            for values in rows:
                row = dict(zip(columns, values, strict=True))
                yield RawRecord(
                    source=self.name,
                    source_record_id=str(row["id"]),
                    name=row["name"],
                    country_iso2=row["country_iso2"],
                    category=row["category"],
                    phone=row["phone"],
                    email=row["email"],
                    website=row["website"],
                    address=row["address"],
                    city=row["city"],
                    region=row["region"],
                    postcode=row["postcode"],
                    lat=row["lat"],
                    lon=row["lon"],
                    raw={"confidence": row["confidence"], "release": self.release},
                )
