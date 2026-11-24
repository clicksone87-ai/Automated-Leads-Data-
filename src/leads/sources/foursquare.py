"""Foursquare Open Source Places (parquet, read with DuckDB).

Docs: https://docs.foursquare.com/data-products/docs/access-fsq-os-places
Provides ``date_created`` / ``date_refreshed`` / ``date_closed`` — useful age evidence.
Licence: Apache 2.0, attribution in exports. See docs/runbook.md for how to get access.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from datetime import date
from typing import Any

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord

FETCH_BATCH = 10_000
COLUMNS = (
    "fsq_place_id", "name", "latitude", "longitude", "address", "locality", "region", "postcode",
    "country", "date_created", "date_refreshed", "date_closed", "tel", "website", "email",
    "facebook_id", "instagram", "twitter", "fsq_category_labels",
)  # fmt: skip


def social_urls(facebook_id: Any, instagram: str | None, twitter: str | None) -> list[str]:
    urls = []
    if facebook_id:
        urls.append(f"https://www.facebook.com/{facebook_id}")
    if instagram:
        urls.append(f"https://www.instagram.com/{instagram.lstrip('@')}")
    if twitter:
        urls.append(f"https://x.com/{twitter.lstrip('@')}")
    return urls


class FoursquareSource:
    name = "foursquare"

    def __init__(self, uri: str | None, hf_token: str | None = None) -> None:
        self.uri = uri
        self.hf_token = hf_token

    def _scan(self) -> str:
        if not self.uri:
            raise ValueError(
                "Foursquare is enabled but sources.foursquare.uri is not set in "
                "config/settings.yaml (see docs/runbook.md)"
            )
        return "read_parquet('" + self.uri.replace("'", "''") + "')"

    def build_query(self) -> str:
        return f"""
            SELECT * REPLACE (array_to_string(fsq_category_labels, ' | ') AS fsq_category_labels)
            FROM (SELECT {", ".join(COLUMNS)} FROM {self._scan()})
            WHERE list_contains($countries, country)
              AND name IS NOT NULL
              AND date_closed IS NULL
        """

    def connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute("SET enable_progress_bar = false")
        if self.uri and self.uri.startswith(("s3://", "hf://", "https://")):
            con.execute("INSTALL httpfs; LOAD httpfs; SET http_timeout = 300; SET http_retries = 8")
        if self.hf_token and self.uri and self.uri.startswith("hf://"):
            token = self.hf_token.replace("'", "''")
            con.execute(f"CREATE SECRET hf (TYPE huggingface, TOKEN '{token}')")
        return con

    def check_columns(self, con: duckdb.DuckDBPyConnection) -> None:
        """Fail early, naming the columns, when a release changed its schema."""
        rows = con.execute(f"DESCRIBE SELECT * FROM {self._scan()}").fetchall()
        missing = sorted(set(COLUMNS) - {r[0] for r in rows})
        if missing:
            raise ValueError(f"Foursquare release is missing columns: {', '.join(missing)}")

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Stream open places for ``countries``. Rows are NOT dropped by ``since`` here:
        ``date_created`` is when the place entered Foursquare, which the age resolver
        combines with other evidence.
        """
        query = self.build_query()
        con = self.connect()
        self.check_columns(con)
        cursor = con.execute(query, {"countries": sorted(countries)})
        columns = [d[0] for d in cursor.description or []]
        while rows := cursor.fetchmany(FETCH_BATCH):
            for values in rows:
                yield self.to_record(dict(zip(columns, values, strict=True)))

    def to_record(self, row: Mapping[str, Any]) -> RawRecord:
        return RawRecord(
            source=self.name,
            source_record_id=str(row["fsq_place_id"]),
            name=row["name"],
            country_iso2=row["country"],
            category=row["fsq_category_labels"],
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
            shared_ids=[f"foursquare:{row['fsq_place_id']}"],
            raw={
                "date_refreshed": row["date_refreshed"],
                "socials": social_urls(row["facebook_id"], row["instagram"], row["twitter"]),
            },
        )
