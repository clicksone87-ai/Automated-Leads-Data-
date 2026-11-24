"""Sanctions screening against the US OFAC SDN list.

A lead is skipped (``sanctions_hit = True``) when its name fuzzy-matches an SDN entry
(score >= ``settings.sanctions.min_score``, default 92) and the entry lists the lead's
country in its addresses. SDN entries with no address country match on name alone
(conservative).
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

from leads.pipeline.normalise import normalise_name

SDN_NULL = "-0-"

# OFAC address files use English country names. TODO(build step 7): complete this map
# for every country in config/countries.yaml (unmapped names are ignored).
COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "united kingdom": "GB",
    "united states": "US",
    "russia": "RU",
    "turkey": "TR",
    "turkiye": "TR",
    "united arab emirates": "AE",
    "syria": "SY",
    "iraq": "IQ",
    "lebanon": "LB",
    "yemen": "YE",
    "germany": "DE",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "cyprus": "CY",
    "georgia": "GE",
    "armenia": "AM",
    "azerbaijan": "AZ",
    "kazakhstan": "KZ",
    "belarus": "BY",
    "ukraine": "UA",
    "mexico": "MX",
    "venezuela": "VE",
    "cuba": "CU",
    "canada": "CA",
}


@dataclass(slots=True)
class SdnEntry:
    ent_num: str
    name: str
    name_norm: str
    sdn_type: str
    countries: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class SanctionsMatch:
    ent_num: str
    sdn_name: str
    score: float


def download_sdn(dest_dir: Path, *, sdn_url: str, add_url: str, timeout: float = 60.0) -> Path:
    """Download SDN.CSV and ADD.CSV into ``dest_dir`` (overwrites). Returns ``dest_dir``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url, name in ((sdn_url, "sdn.csv"), (add_url, "add.csv")):
            response = client.get(url)
            response.raise_for_status()
            (dest_dir / name).write_bytes(response.content)
    return dest_dir


def _clean(value: str) -> str:
    value = value.strip()
    return "" if value == SDN_NULL else value


def load_sdn(sdn_csv: Path, add_csv: Path | None = None) -> list[SdnEntry]:
    """Parse OFAC SDN.CSV (no header: ent_num, SDN_Name, SDN_Type, ...) and ADD.CSV
    (ent_num, add_num, address, city/state/postal, country, remarks)."""
    countries: dict[str, set[str]] = defaultdict(set)
    if add_csv and add_csv.exists():
        with add_csv.open(encoding="latin-1", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) >= 5 and (iso := COUNTRY_NAME_TO_ISO2.get(_clean(row[4]).lower())):
                    countries[row[0].strip()].add(iso)
    entries: list[SdnEntry] = []
    with sdn_csv.open(encoding="latin-1", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 3 or not _clean(row[1]):
                continue
            ent = row[0].strip()
            name = _clean(row[1])
            entries.append(
                SdnEntry(
                    ent_num=ent,
                    name=name,
                    name_norm=normalise_name(name),
                    sdn_type=_clean(row[2]),
                    countries=countries.get(ent, set()),
                )
            )
    return entries


def match_sanctions(
    name: str, country_iso2: str | None, entries: Iterable[SdnEntry], *, min_score: int = 92
) -> SanctionsMatch | None:
    """Best SDN match for a business name in the same country (or a country-less entry)."""
    name_norm = normalise_name(name)
    if not name_norm:
        return None
    pool = [e for e in entries if not e.countries or (country_iso2 and country_iso2 in e.countries)]
    if not pool:
        return None
    best = process.extractOne(
        name_norm, [e.name_norm for e in pool], scorer=fuzz.token_sort_ratio, score_cutoff=min_score
    )
    if best is None:
        return None
    _, score, index = best
    entry = pool[index]
    return SanctionsMatch(ent_num=entry.ent_num, sdn_name=entry.name, score=float(score))


def screen_leads(*, refresh_list: bool = True) -> dict[str, int]:
    """Download the SDN list (daily) and set ``leads.sanctions_hit`` for matching leads.

    TODO(build step 7):
      1. ``download_sdn(settings.data_dir / "sanctions", ...)`` when the local copy is
         older than 24 h (or ``refresh_list``).
      2. ``load_sdn`` once; index entries by country to keep matching fast.
      3. Stream active leads (id, name, country) in chunks; ``match_sanctions`` each.
      4. Bulk-update ``sanctions_hit`` (true for hits, false otherwise) and log hits.
      5. Return counts: {"screened": n, "hits": n}.
    """
    raise NotImplementedError("sanctions.screen_leads (build step 7)")
