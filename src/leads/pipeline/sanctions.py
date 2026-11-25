"""Sanctions screening against the US OFAC SDN list.

A lead is skipped (``sanctions_hit = True``) when its name fuzzy-matches an SDN entry
(score >= ``settings.sanctions.min_score``, default 92) and the entry lists the lead's
country in its addresses. SDN entries with no address country match on name alone
(conservative).
"""

from __future__ import annotations

import csv
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
from rapidfuzz import fuzz, process
from sqlalchemy import select, update

from leads.db import lock_leads, session_scope
from leads.models import Lead, LeadStatus
from leads.pipeline.normalise import normalise_name
from leads.settings import Settings, get_settings

log = structlog.get_logger(__name__)

SDN_NULL = "-0-"
# Ships and planes share names such as "Premier" or "Amber" with ordinary shops.
NOT_BUSINESSES = frozenset({"vessel", "aircraft"})

# OFAC address files use English country names. A name missing here leaves the entry without
# a country, so it matches everywhere.
COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "united kingdom": "GB",
    "united states": "US",
    "ireland": "IE",
    "malta": "MT",
    "netherlands": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "norway": "NO",
    "sweden": "SE",
    "australia": "AU",
    "china": "CN",
    "hong kong": "HK",
    "iran": "IR",
    "pakistan": "PK",
    "afghanistan": "AF",
    "colombia": "CO",
    "panama": "PA",
    "north korea": "KP",
    "korea, north": "KP",
    "singapore": "SG",
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
            if len(row) < 3 or not _clean(row[1]) or _clean(row[2]).lower() in NOT_BUSINESSES:
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


class Screen:
    """The loaded list, indexed by country so one lookup stays fast."""

    def __init__(self, entries: Iterable[SdnEntry], min_score: int) -> None:
        self._min = min_score
        self._by_country: dict[str, dict[str, SdnEntry]] = defaultdict(dict)
        for entry in entries:
            for country in entry.countries or {""}:
                self._by_country[country][entry.name_norm] = entry

    def match(self, name: str, country_iso2: str | None) -> SanctionsMatch | None:
        wanted = normalise_name(name)
        if not wanted:
            return None
        for country in {"", country_iso2 or ""}:
            pool = self._by_country.get(country)
            if not pool:
                continue
            best = process.extractOne(
                wanted, pool.keys(), scorer=fuzz.token_sort_ratio, score_cutoff=self._min
            )
            if best:
                entry = pool[best[0]]
                return SanctionsMatch(entry.ent_num, entry.name, float(best[1]))
        return None


_loaded: tuple[float, Screen] | None = None


def current_screen(settings: Settings, *, refresh: bool = False) -> Screen:
    """Today's list, downloaded when the local copy is older than a day.

    A failed download falls back to a copy of up to a week; without one nothing is screened
    and the caller must stop.
    """
    global _loaded
    directory = settings.data_dir / "sanctions"
    sdn = directory / "sdn.csv"
    age = time.time() - sdn.stat().st_mtime if sdn.exists() else float("inf")
    if refresh or age > 86_400:
        try:
            download_sdn(
                directory, sdn_url=settings.sanctions.sdn_url, add_url=settings.sanctions.add_url
            )
        except httpx.HTTPError as exc:
            if age > 7 * 86_400:
                raise RuntimeError("The sanctions list could not be downloaded") from exc
    stamp = sdn.stat().st_mtime
    if _loaded is None or _loaded[0] != stamp:
        entries = load_sdn(sdn, directory / "add.csv")
        if len(entries) < 1000:
            raise RuntimeError("The sanctions list is incomplete")
        _loaded = (stamp, Screen(entries, settings.sanctions.min_score))
    return _loaded[1]


def screen_leads(*, refresh_list: bool = True) -> dict[str, int]:
    """Screen every active lead against the sanctions list and store the result."""
    screen = current_screen(get_settings(), refresh=refresh_list)
    hits: list[str] = []
    screened = 0
    with session_scope() as session:
        stmt = select(Lead.lead_id, Lead.name, Lead.country_iso2).where(
            Lead.status == LeadStatus.ACTIVE
        )
        for lead_id, name, country in session.execute(stmt).yield_per(5000):
            screened += 1
            if found := screen.match(name, country):
                hits.append(lead_id)
                log.info("sanctions_hit", lead_id=lead_id, name=name, listed=found.sdn_name)
    with session_scope() as session:
        lock_leads(session)
        session.execute(
            update(Lead)
            .where(Lead.sanctions_hit.is_(True), Lead.lead_id.not_in(hits))
            .values(sanctions_hit=False)
        )
        session.execute(update(Lead).where(Lead.lead_id.in_(hits)).values(sanctions_hit=True))
    return {"screened": screened, "hits": len(hits)}
