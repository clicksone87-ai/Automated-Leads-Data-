"""Overture Maps Places (GeoParquet on S3, read with DuckDB).

Docs: https://docs.overturemaps.org/guides/places/ — no creation dates in Overture, so
age comes from other sources. Attribution required (see leads.export.ATTRIBUTION).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import date
from typing import Any
from xml.etree import ElementTree

import duckdb
import httpx

from leads.sources.base import RawRecord, bbox_sql, country_batches

FETCH_BATCH = 10_000
DEFAULT_BASE_URI = "s3://overturemaps-us-west-2/release"
RELEASES_URL = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
CLOSED_STATUSES = ("permanently_closed",)


def parse_releases(listing_xml: str) -> list[str]:
    """Release names in an S3 ``release/`` listing, oldest first."""
    names = []
    for node in ElementTree.fromstring(listing_xml).iter():
        if node.tag.endswith("Prefix") and node.text and node.text.startswith("release/"):
            name = node.text.removeprefix("release/").strip("/")
            if name:
                names.append(name)
    return sorted(names)


def latest_release(*, timeout: float = 60.0) -> str:
    """Newest Overture release published on S3."""
    response = httpx.get(
        RELEASES_URL,
        params={"list-type": "2", "prefix": "release/", "delimiter": "/"},
        timeout=timeout,
    )
    response.raise_for_status()
    releases = parse_releases(response.text)
    if not releases:
        raise RuntimeError("No Overture releases found on S3")
    return releases[-1]


def foursquare_ids(sources: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """Foursquare ids among a place's Overture ``sources``, as shared match ids."""
    return sorted(
        {
            f"foursquare:{s['record_id']}"
            for s in sources or ()
            if (s.get("dataset") or "").lower() == "foursquare" and s.get("record_id")
        }
    )


class OvertureSource:
    name = "overture"

    def __init__(
        self,
        release: str | None,
        base_uri: str = DEFAULT_BASE_URI,
        bbox: Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        self.release = release
        self.base_uri = base_uri.rstrip("/")
        self.bbox = {str(k).upper(): tuple(v) for k, v in (bbox or {}).items()}

    def places_uri(self) -> str:
        if not self.release:
            self.release = latest_release()
        return f"{self.base_uri}/{self.release}/theme=places/type=place/*"

    def build_query(self, countries: Collection[str] = ()) -> str:
        """SQL selecting the fields we need; country list is bound as parameter ``$countries``."""
        closed = ", ".join(f"'{s}'" for s in CLOSED_STATUSES)
        return f"""
            SELECT
                id,
                names.primary                              AS name,
                COALESCE(taxonomy.primary, basic_category) AS category,
                websites[1]                                AS website,
                phones[1]                                  AS phone,
                emails[1]                                  AS email,
                socials,
                addresses[1].freeform                      AS address,
                addresses[1].locality                      AS city,
                addresses[1].region                        AS region,
                addresses[1].postcode                      AS postcode,
                addresses[1].country                       AS country_iso2,
                bbox.xmin                                  AS lon,
                bbox.ymin                                  AS lat,
                taxonomy.hierarchy                         AS category_path,
                confidence,
                operating_status,
                sources
            FROM read_parquet('{self.places_uri()}', hive_partitioning = 1)
            WHERE list_contains($countries, addresses[1].country)
              AND names.primary IS NOT NULL
              AND COALESCE(operating_status, 'open') NOT IN ({closed})
              {bbox_sql(self.bbox, countries, "bbox.xmin", "bbox.ymin")}
        """

    def connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute(
            "INSTALL httpfs; LOAD httpfs; SET s3_region = 'us-west-2'; "
            "SET http_timeout = 300; SET http_retries = 8; SET enable_progress_bar = false;"
        )
        return con

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Stream places for ``countries``. ``since`` is unused (Overture has no dates)."""
        con = self.connect()
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
            shared_ids=foursquare_ids(row["sources"]),
            raw={
                "confidence": row["confidence"],
                "operating_status": row["operating_status"],
                "release": self.release,
                "category_path": list(row["category_path"] or ()),
                "socials": list(row["socials"] or ()),
            },
        )
