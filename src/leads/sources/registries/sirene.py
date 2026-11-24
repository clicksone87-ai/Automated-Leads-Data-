"""France SIRENE (INSEE): monthly parquet files on data.gouv.fr, read in place with DuckDB.

Establishments that opted out of publication are skipped, and a sole trader is included
only under a shop sign or trading name, never under the person's own name.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord
from leads.sources.registries.download import get_text

DATASET_URL = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "base-sirene-des-entreprises-et-de-leurs-etablissements-siren-siret/"
)
FETCH_BATCH = 10_000
OVERSEAS = {
    "971": "GP", "972": "MQ", "973": "GF", "974": "RE", "975": "PM", "976": "YT",
    "977": "BL", "978": "MF", "986": "WF", "987": "PF", "988": "NC",
}  # fmt: skip


def bulk_urls(dataset_json: str) -> tuple[str, str]:
    """(establishments, legal units) parquet URLs, newest first in the dataset."""
    urls = [r["url"] for r in json.loads(dataset_json)["resources"]]

    def newest(stem: str) -> str:
        found = [u for u in urls if u.endswith(f"stock-{stem}-parquet.parquet")]
        if not found:
            raise RuntimeError(f"SIRENE {stem} parquet not found on data.gouv.fr")
        return max(found)

    return newest("stocketablissement"), newest("stockunitelegale")


def country_of(commune_code: str | None) -> str:
    return OVERSEAS.get((commune_code or "")[:3], "FR")


class SireneSource:
    name = "sirene"
    country_iso2 = "FR"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "sirene"

    def query(self, sites: str, units: str) -> str:
        return f"""
            SELECT e.siret, e.siren,
                COALESCE(NULLIF(e.enseigne1Etablissement, ''),
                         NULLIF(e.denominationUsuelleEtablissement, ''),
                         NULLIF(u.denominationUsuelle1UniteLegale, ''),
                         u.denominationUniteLegale) AS name,
                e.activitePrincipaleEtablissement AS activity,
                e.libelleCommuneEtablissement AS city,
                e.codePostalEtablissement AS postcode,
                e.codeCommuneEtablissement AS commune,
                LEAST(u.dateCreationUniteLegale, e.dateCreationEtablissement) AS created
            FROM read_parquet('{sites}') e JOIN read_parquet('{units}') u USING (siren)
            WHERE e.etatAdministratifEtablissement = 'A'
              AND e.statutDiffusionEtablissement = 'O'
              AND u.statutDiffusionUniteLegale = 'O'
        """

    def connect(self) -> duckdb.DuckDBPyConnection:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        # The join is larger than memory on a laptop: let it spill to disk.
        con.execute(
            "INSTALL httpfs; LOAD httpfs; SET http_timeout = 300; SET http_retries = 8; "
            "SET enable_progress_bar = false; SET memory_limit = '4GB'; "
            "SET preserve_insertion_order = false"
        )
        con.execute(f"SET temp_directory = '{self.data_dir / 'tmp'}'")
        return con

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active establishments of any age: an old date proves one is too old."""
        wanted = set(countries) & {self.country_iso2, *OVERSEAS.values()}
        if not wanted:
            return
        sites, units = bulk_urls(get_text(DATASET_URL))
        cursor = self.connect().execute(self.query(sites, units))
        columns = [d[0] for d in cursor.description or []]
        while batch := cursor.fetchmany(FETCH_BATCH):
            for values in batch:
                row = dict(zip(columns, values, strict=True))
                if row["name"] and country_of(row["commune"]) in wanted:
                    yield self.to_record(row)

    def to_record(self, row: Mapping[str, Any]) -> RawRecord:
        return RawRecord(
            source=self.name,
            source_record_id=row["siret"],
            name=row["name"],
            country_iso2=country_of(row["commune"]),
            category=row["activity"],
            city=row["city"],
            postcode=row["postcode"],
            created_date=parse_date_loose(row["created"]),
            raw={"siren": row["siren"]},
        )
