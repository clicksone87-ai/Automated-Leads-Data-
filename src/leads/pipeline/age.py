"""Age resolver: estimate when a business was created and apply the ``since`` filter.

``created_date`` = earliest of: registry incorporation date, Foursquare ``date_created``,
OSM first version, Wayback Machine first capture, "N years in business" text.
If none of those exist, ``first_seen`` (when this tool first saw it) is used as the date
but the lead is flagged ``age_unknown = True``, because first_seen says nothing about
how old the business really is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import httpx

from leads.models import LeadStatus


@dataclass(frozen=True, slots=True)
class AgeEvidence:
    registry: date | None = None
    fsq_date_created: date | None = None
    osm_first_version: date | None = None
    wayback_first_capture: date | None = None
    years_in_business: int | None = None
    first_seen: date | None = None


@dataclass(frozen=True, slots=True)
class AgeResult:
    created_date: date | None
    age_source: str | None
    age_unknown: bool


def years_in_business_to_date(years: int | None, today: date) -> date | None:
    """ "5 years in business" seen in 2026 -> 2021-01-01. Implausible values -> None."""
    if years is None or years < 0 or years > 300:
        return None
    return date(today.year - years, 1, 1)


def parse_date_loose(value: str | date | datetime | None) -> date | None:
    """Accept ``date``, ``datetime``, ``YYYY-MM-DD``, ``YYYYMMDD``, ``YYYY-MM`` or ``YYYY``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt).date()
        except ValueError:
            continue
    return None


def resolve_age(evidence: AgeEvidence, *, today: date | None = None) -> AgeResult:
    """Pick the earliest real evidence; fall back to first_seen with age_unknown=True."""
    today = today or date.today()
    candidates = [
        (d, name)
        for name, d in (
            ("registry", evidence.registry),
            ("foursquare", evidence.fsq_date_created),
            ("osm", evidence.osm_first_version),
            ("wayback", evidence.wayback_first_capture),
            ("years_in_business", years_in_business_to_date(evidence.years_in_business, today)),
        )
        if d is not None and d <= today
    ]
    if candidates:
        created, source = min(candidates)
        return AgeResult(created_date=created, age_source=source, age_unknown=False)
    if evidence.first_seen is not None:
        return AgeResult(
            created_date=evidence.first_seen, age_source="first_seen", age_unknown=True
        )
    return AgeResult(created_date=None, age_source=None, age_unknown=True)


def keep_lead(status: str, created_date: date | None, age_unknown: bool, since: date) -> bool:
    """Age filter for the DB: not closed, and created on/after ``since`` (unknown age is kept)."""
    if status == LeadStatus.CLOSED:
        return False
    if age_unknown:
        return True
    return created_date is not None and created_date >= since


def eligible_for_outreach(
    status: str,
    created_date: date | None,
    age_unknown: bool,
    since: date,
    *,
    include_unknown_age: bool,
) -> bool:
    """Age filter for exports, the Sheet and drafts (``settings.include_unknown_age``)."""
    if not keep_lead(status, created_date, age_unknown, since):
        return False
    return include_unknown_age or not age_unknown


def wayback_first_capture(
    domain: str, *, client: httpx.Client, timeout: float = 12.0
) -> date | None:
    """Earliest Wayback Machine capture of ``domain`` via the CDX API (None if never archived).

    TODO(build step 3): call this only for leads still missing real age evidence, cache
    results, and stay well under archive.org rate limits.
    """
    response = client.get(
        "https://web.archive.org/cdx/search/cdx",
        params={"url": domain, "output": "json", "fl": "timestamp", "limit": "1"},
        timeout=timeout,
    )
    response.raise_for_status()
    rows = response.json()
    if len(rows) < 2:  # first row is the header
        return None
    return parse_date_loose(str(rows[1][0])[:8])
