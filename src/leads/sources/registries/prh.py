"""Finland Patent and Registration Office (PRH / YTJ): every company on the Trade Register.

Free open data, updated daily: one zipped JSON file of all registered companies
(https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies). Contact details are not part
of the open data, apart from a website that some companies list.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord
from leads.sources.registries.download import download

BULK_URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies"
FETCH_BATCH = 5_000
REGISTERED = "1"  # tradeRegisterStatus of a company that is on the Trade Register
FINNISH, ENGLISH = "1", "3"  # languageCode values
VISITING_ADDRESS = 1


def _current(entries: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    """Entries still in force (no end date), newest registration first."""
    found = [e for e in entries or [] if not e.get("endDate")]
    return sorted(found, key=lambda e: str(e.get("registrationDate") or ""), reverse=True)


def _text(descriptions: Sequence[Mapping[str, Any]] | None, language: str) -> str | None:
    for item in descriptions or []:
        if item.get("languageCode") == language:
            return str(item.get("description") or "") or None
    return None


def to_record(company: Mapping[str, Any]) -> RawRecord | None:
    """One company, or None when it is not an active registered business."""
    if company.get("tradeRegisterStatus") != REGISTERED or company.get("companySituations"):
        return None  # bankruptcy, liquidation or restructuring
    names = _current(company.get("names"))
    business_id = (company.get("businessId") or {}).get("value")
    if not names or not business_id:
        return None
    addresses = _current(company.get("addresses"))
    addresses.sort(key=lambda a: a.get("type") != VISITING_ADDRESS)
    address = addresses[0] if addresses else {}
    street = " ".join(
        str(x) for x in (address.get("street"), address.get("buildingNumber")) if x
    ).strip()
    city = next(
        (
            p.get("city")
            for p in address.get("postOffices") or []
            if p.get("languageCode") == FINNISH
        ),
        None,
    )
    line = company.get("mainBusinessLine") or {}
    category = " ".join(
        x for x in (line.get("type"), _text(line.get("descriptions"), ENGLISH)) if x
    )
    dates = [
        d
        for d in (
            parse_date_loose((company.get("businessId") or {}).get("registrationDate")),
            parse_date_loose(company.get("registrationDate")),
        )
        if d
    ]
    forms = _current(company.get("companyForms"))
    website = (company.get("website") or {}).get("url")
    return RawRecord(
        source=PrhSource.name,
        source_record_id=str(business_id),
        name=str(names[0]["name"]),
        country_iso2=PrhSource.country_iso2,
        category=category or None,
        website=str(website) if website else None,
        address=street or None,
        city=str(city).title() if city else None,
        postcode=address.get("postCode") or None,
        created_date=min(dates, default=None),
        shared_ids=[f"prh:{business_id}"],
        raw={"form": _text(forms[0].get("descriptions"), ENGLISH) if forms else None},
    )


class PrhSource:
    name = "prh"
    country_iso2 = "FI"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "prh"

    def json_file(self) -> Path:
        archive = download(BULK_URL, self.data_dir / "all_companies.zip")
        target = self.data_dir / "all_companies.json"
        if not target.exists() or target.stat().st_mtime < archive.stat().st_mtime:
            with zipfile.ZipFile(archive) as zf:
                member = next(n for n in zf.namelist() if n.endswith(".json"))
                part = target.with_name(target.name + ".part")
                with zf.open(member) as src, part.open("wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                part.replace(target)
        return target

    def companies(self, path: Path) -> Iterator[dict[str, Any]]:
        cursor = duckdb.connect().execute(
            f"SELECT json FROM read_json_objects('{path}', format = 'array')"
        )
        while batch := cursor.fetchmany(FETCH_BATCH):
            for (text,) in batch:
                yield json.loads(text)

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for company in self.companies(self.json_file()):
            if (record := to_record(company)) is not None:
                yield record
