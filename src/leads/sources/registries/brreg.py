"""Norway Brønnøysund Register Centre (Enhetsregisteret), bulk CSV, open data (NLOD).

Organisations ("enheter") carry the founding date; establishments ("underenheter") carry the
trading address. Many rows list an email, phone and website.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord
from leads.sources.registries.download import download

BULK_URL = "https://data.brreg.no/enhetsregisteret/api/{kind}/lastned/csv"
FETCH_BATCH = 10_000
# Public bodies, associations, churches, housing co-operatives, estates, foundations.
NON_BUSINESS_FORMS = (
    "ADOS", "ANNA", "BBL", "BRL", "ESEK", "FLI", "FYLK", "IKS", "KBO", "KF", "KIRK", "KOMM",
    "KTRF", "ORGL", "PERS", "SAM", "SF", "SPA", "STAT", "STI", "SÆR", "TVAM", "UTBG", "VIFE",
    "AAFY", "OPMV",
)  # fmt: skip


class BrregSource:
    name = "brreg"
    country_iso2 = "NO"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "brreg"

    def files(self) -> tuple[Path, Path]:
        paths = [
            download(BULK_URL.format(kind=kind), self.data_dir / f"{kind}.csv.gz")
            for kind in ("enheter", "underenheter")
        ]
        return paths[0], paths[1]

    def query(self, units: Path, sites: Path) -> str:
        def scan(path: Path) -> str:
            return (
                f"read_csv('{path}', header = true, all_varchar = true, quote = '\"', "
                "escape = '\"', compression = 'gzip')"
            )

        forms = ", ".join(f"'{f}'" for f in NON_BUSINESS_FORMS)
        return f"""
            WITH units AS (
                SELECT *, LEAST(
                    TRY_CAST(stiftelsesdato AS DATE),
                    TRY_CAST(registreringsdatoenhetsregisteret AS DATE)
                ) AS founded
                FROM {scan(units)}
                WHERE "organisasjonsform.kode" NOT IN ({forms})
                  AND konkurs = 'false' AND underAvvikling = 'false'
                  AND underTvangsavviklingEllerTvangsopplosning = 'false'
            ), sites AS (
                SELECT * FROM {scan(sites)} WHERE nedleggelsesdato IS NULL OR nedleggelsesdato = ''
            )
            SELECT s.organisasjonsnummer AS number, u.organisasjonsnummer AS parent,
                s.navn AS name, s."naeringskode1.kode" AS code,
                s."naeringskode1.beskrivelse" AS activity,
                COALESCE(NULLIF(s.hjemmeside, ''), u.hjemmeside) AS website,
                COALESCE(NULLIF(s.epostadresse, ''), u.epostadresse) AS email,
                COALESCE(NULLIF(s.telefon, ''), NULLIF(s.mobil, ''),
                         NULLIF(u.telefon, ''), u.mobil) AS phone,
                s."beliggenhetsadresse.adresse" AS address,
                s."beliggenhetsadresse.poststed" AS city,
                s."beliggenhetsadresse.kommune" AS region,
                s."beliggenhetsadresse.postnummer" AS postcode,
                u.founded, TRY_CAST(s.oppstartsdato AS DATE) AS started,
                u."organisasjonsform.kode" AS form
            FROM sites s JOIN units u ON u.organisasjonsnummer = s.overordnetEnhet
            UNION ALL
            SELECT u.organisasjonsnummer, u.organisasjonsnummer, u.navn, u."naeringskode1.kode",
                u."naeringskode1.beskrivelse", u.hjemmeside, u.epostadresse,
                COALESCE(NULLIF(u.telefon, ''), u.mobil), u."forretningsadresse.adresse",
                u."forretningsadresse.poststed", u."forretningsadresse.kommune",
                u."forretningsadresse.postnummer", u.founded, NULL, u."organisasjonsform.kode"
            FROM units u ANTI JOIN sites s ON u.organisasjonsnummer = s.overordnetEnhet
            WHERE u."forretningsadresse.landkode" = 'NO'
        """

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active businesses of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        cursor = duckdb.connect().execute(self.query(*self.files()))
        columns = [d[0] for d in cursor.description or []]
        while batch := cursor.fetchmany(FETCH_BATCH):
            for values in batch:
                row = dict(zip(columns, values, strict=True))
                if row["name"]:
                    yield self.to_record(row)

    def to_record(self, row: Mapping[str, Any]) -> RawRecord:
        # A sole trader's address is a private person's: emailing it needs their consent.
        email = None if row["form"] == "ENK" else row["email"]
        # A new site of an old company is not a new business.
        dates = [
            d for d in (parse_date_loose(row["founded"]), parse_date_loose(row["started"])) if d
        ]
        category = " ".join(x for x in (row["code"], row["activity"]) if x)
        return RawRecord(
            source=self.name,
            source_record_id=row["number"],
            name=row["name"],
            country_iso2=self.country_iso2,
            category=category or None,
            phone=row["phone"] or None,
            email=email or None,
            website=row["website"] or None,
            address=row["address"] or None,
            city=row["city"] or None,
            region=row["region"] or None,
            postcode=row["postcode"] or None,
            created_date=min(dates, default=None),
            raw={"parent": row["parent"], "form": row["form"]},
        )
