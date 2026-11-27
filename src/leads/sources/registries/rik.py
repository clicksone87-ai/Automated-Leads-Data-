"""Estonia e-Business Register (RIK): basic data of every registered company.

Free open data, updated daily (https://avaandmed.ariregister.rik.ee/en/downloading-open-data):
a zipped, semicolon-separated CSV with the name, legal form, status, first entry date and
address. No phone, email or website.
"""

from __future__ import annotations

import zipfile
from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.sources.base import RawRecord
from leads.sources.registries.download import download

BULK_URL = (
    "https://avaandmed.ariregister.rik.ee/sites/default/files/avaandmed/"
    "ettevotja_rekvisiidid__lihtandmed.csv.zip"
)
FETCH_BATCH = 10_000
REGISTERED = "R"  # in liquidation (L) and bankrupt (N) companies are left out
# Private limited company, public limited company, sole trader, general partnership,
# limited partnership, commercial association, branch of a foreign company. Non-profits,
# foundations, apartment associations and public bodies are not leads.
BUSINESS_FORMS = (
    "Osaühing", "Aktsiaselts", "Füüsilisest isikust ettevõtja", "Täisühing", "Usaldusühing",
    "Tulundusühistu", "Välismaa äriühingu filiaal",
)  # fmt: skip


def city_of(place: str | None) -> str | None:
    """``"Pirita linnaosa, Tallinn, Harju maakond"`` -> ``"Tallinn"``."""
    parts = [p.strip() for p in (place or "").split(",") if p.strip()]
    if parts and parts[-1].endswith("maakond"):
        parts.pop()
    return parts[-1] if parts else None


class RikSource:
    name = "rik"
    country_iso2 = "EE"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "rik"

    def csv_file(self) -> Path:
        archive = download(BULK_URL, self.data_dir / "lihtandmed.csv.zip")
        target = self.data_dir / "lihtandmed.csv"
        if not target.exists() or target.stat().st_mtime < archive.stat().st_mtime:
            with zipfile.ZipFile(archive) as zf:
                member = next(n for n in zf.namelist() if n.endswith(".csv"))
                part = target.with_name(target.name + ".part")
                part.write_bytes(zf.read(member))
                part.replace(target)
        return target

    def rows(self, path: Path) -> Iterator[dict[str, Any]]:
        forms = ", ".join(f"'{f}'" for f in BUSINESS_FORMS)
        cursor = duckdb.connect().execute(
            f"""
            SELECT ariregistri_kood AS number, nimi AS name,
                ettevotja_oiguslik_vorm AS form,
                TRY_STRPTIME(ettevotja_esmakande_kpv, '%d.%m.%Y')::DATE AS registered,
                asukoht_ettevotja_aadressis AS address,
                asukoha_ehak_tekstina AS place,
                indeks_ettevotja_aadressis AS postcode
            FROM read_csv('{path}', header = true, delim = ';', all_varchar = true,
                          quote = '"')
            WHERE ettevotja_staatus = '{REGISTERED}' AND ettevotja_oiguslik_vorm IN ({forms})
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
            address=row["address"] or None,
            city=city_of(row["place"]),
            postcode=row["postcode"] or None,
            created_date=row["registered"],
            shared_ids=[f"rik:{row['number']}"],
            raw={"form": row["form"]},
        )

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for row in self.rows(self.csv_file()):
            if row["name"] and row["number"]:
                yield self.to_record(row)
