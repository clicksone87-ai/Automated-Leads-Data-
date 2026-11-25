"""Foursquare Open Source Places (parquet, read with DuckDB).

Docs: https://docs.foursquare.com/data-products/docs/access-fsq-os-places
Provides ``date_created`` / ``date_refreshed`` / ``date_closed`` — useful age evidence.
Licence: Apache 2.0, attribution in exports. See docs/runbook.md for how to get access.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import date
from typing import Any

import duckdb
import httpx

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord, bbox_sql, country_batches

FETCH_BATCH = 10_000
COLUMNS = (
    "fsq_place_id", "name", "latitude", "longitude", "address", "locality", "region", "postcode",
    "country", "date_created", "date_refreshed", "date_closed", "tel", "website", "email",
    "facebook_id", "instagram", "twitter", "fsq_category_labels", "unresolved_flags",
)  # fmt: skip


DATASET = "datasets/foursquare/fsq-os-places"
# Reports by Foursquare users that the place is not a real, open, public business.
BAD_FLAGS = ("closed", "delete", "doesnt_exist", "duplicate", "inappropriate", "privatevenue")


def latest_release_uri(hf_token: str, *, timeout: float = 60.0) -> str:
    """Parquet glob of the newest release in the gated Hugging Face dataset."""
    response = httpx.get(
        f"https://huggingface.co/api/{DATASET}/tree/main/release",
        headers={"Authorization": f"Bearer {hf_token}"},
        timeout=timeout,
    )
    response.raise_for_status()
    releases = sorted(x["path"] for x in response.json() if x["path"].startswith("release/dt="))
    if not releases:
        raise RuntimeError("No Foursquare releases found on Hugging Face")
    return f"hf://{DATASET}/{releases[-1]}/places/parquet/*.parquet"


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

    def __init__(
        self,
        uri: str | None,
        hf_token: str | None = None,
        bbox: Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        self.uri = uri
        self.hf_token = hf_token
        self.bbox = bbox or {}

    def _scan(self) -> str:
        if not self.uri:
            if not self.hf_token:
                raise ValueError(
                    "Foursquare needs HF_TOKEN in .env, or sources.foursquare.uri in "
                    "config/settings.yaml (see docs/runbook.md)"
                )
            self.uri = latest_release_uri(self.hf_token)
        return "read_parquet('" + self.uri.replace("'", "''") + "')"

    def build_query(self, countries: Collection[str] = ()) -> str:
        flags = ", ".join(f"'{f}'" for f in BAD_FLAGS)
        return f"""
            SELECT * EXCLUDE (unresolved_flags)
                REPLACE (array_to_string(fsq_category_labels, ' | ') AS fsq_category_labels)
            FROM (SELECT {", ".join(COLUMNS)} FROM {self._scan()})
            WHERE list_contains($countries, country)
              AND name IS NOT NULL
              AND date_closed IS NULL
              AND NOT COALESCE(list_has_any(unresolved_flags, [{flags}]), false)
              {bbox_sql(self.bbox, countries, "longitude", "latitude")}
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
        self._scan()  # finds the release, which connect() needs to set up access
        con = self.connect()
        self.check_columns(con)
        # One box per query: files can be skipped for a single box, not for many joined by OR.
        for part in country_batches(self.bbox, countries):
            cursor = con.execute(self.build_query(part), {"countries": part})
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
