"""Read queries behind the dashboard."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select, text
from sqlalchemy.orm import Session

from leads.models import Lead, LeadSource, LeadStatus, Outreach, Run, Suppression

PAGE_SIZE = 50
LIST_COLUMNS = (
    "lead_id", "name", "category", "category_group", "country_iso2", "city", "phone_e164",
    "email", "website", "domain", "website_status", "website_reason", "website_checked_at",
    "created_date", "age_source", "age_unknown", "sources",
)  # fmt: skip
SORTS: dict[str, tuple[Any, ...]] = {
    "newest": (Lead.age_unknown, Lead.created_date.desc().nulls_last(), Lead.lead_id),
    "oldest": (Lead.age_unknown, Lead.created_date.asc().nulls_last(), Lead.lead_id),
    "name": (Lead.name, Lead.lead_id),
    "city": (Lead.city.asc().nulls_last(), Lead.lead_id),
}


@dataclass(frozen=True, slots=True)
class LeadFilter:
    q: str = ""
    country: str = ""
    category: str = ""
    city: str = ""
    source: str = ""
    year: int | None = None
    age: str = ""  # known | unknown
    email: str = ""  # yes | no
    phone: str = ""  # yes | no
    website: str = ""  # listed | not_listed | unchecked | a WebsiteStatus value


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _present(column: Any, choice: str) -> list[ColumnElement[bool]]:
    if choice == "yes":
        return [column.is_not(None)]
    return [column.is_(None)] if choice == "no" else []


def conditions(f: LeadFilter) -> list[ColumnElement[bool]]:
    """WHERE clauses for a filter. Closed and sanctioned businesses never show."""
    where: list[ColumnElement[bool]] = [
        Lead.status == LeadStatus.ACTIVE,
        Lead.sanctions_hit.is_(False),
    ]
    if term := f.q.strip():
        pattern = _like(term)
        where.append(
            or_(
                Lead.name.ilike(pattern, escape="\\"),
                Lead.email.ilike(pattern, escape="\\"),
                Lead.domain.ilike(pattern, escape="\\"),
                Lead.phone_e164.ilike(pattern, escape="\\"),
                Lead.postcode.ilike(pattern, escape="\\"),
            )
        )
    if f.country:
        where.append(Lead.country_iso2 == f.country.upper())
    if f.category:
        where.append(Lead.category_group == f.category)
    if f.city.strip():
        where.append(Lead.city.ilike(_like(f.city.strip()), escape="\\"))
    if f.source:
        where.append(Lead.sources.contains([f.source]))
    if f.year:
        where.append(Lead.age_unknown.is_(False))
        where.append(Lead.created_date.between(date(f.year, 1, 1), date(f.year, 12, 31)))
    if f.age in ("known", "unknown"):
        where.append(Lead.age_unknown.is_(f.age == "unknown"))
    where += _present(Lead.email, f.email)
    where += _present(Lead.phone_e164, f.phone)
    if f.website == "listed":
        where.append(Lead.website.is_not(None))
    elif f.website == "not_listed":
        where.append(Lead.website.is_(None))
    elif f.website == "unchecked":
        where.append(Lead.website_checked_at.is_(None))
    elif f.website == "unsure":
        where.append(Lead.website_status.is_(None))
        where.append(Lead.website_checked_at.is_not(None))
    elif f.website:
        where.append(Lead.website_status == f.website)
    return where


def _rows(f: LeadFilter, sort: str, names: tuple[str, ...]) -> Select[Any]:
    return (
        select(*(getattr(Lead, n) for n in names))
        .where(and_(*conditions(f)))
        .order_by(*SORTS.get(sort, SORTS["newest"]))
    )


def list_leads(session: Session, f: LeadFilter, sort: str, page: int) -> dict[str, Any]:
    total = session.scalar(select(func.count()).select_from(Lead).where(and_(*conditions(f))))
    stmt = _rows(f, sort, LIST_COLUMNS).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    return {
        "total": total or 0,
        "page": page,
        "page_size": PAGE_SIZE,
        "leads": [_lead_row(dict(r)) for r in session.execute(stmt).mappings()],
    }


def stream_leads(
    session: Session, f: LeadFilter, sort: str, names: tuple[str, ...]
) -> Iterator[dict[str, Any]]:
    for row in session.execute(_rows(f, sort, names)).yield_per(2000).mappings():
        yield _lead_row(dict(row))


def _lead_row(row: dict[str, Any]) -> dict[str, Any]:
    # An unknown age stores the day we first saw the business, which is not its opening date.
    if row.get("age_unknown"):
        row["created_date"] = None
    return row


def lead_detail(session: Session, lead_id: str) -> dict[str, Any] | None:
    lead = session.get(Lead, lead_id)
    if lead is None:
        return None
    columns = (
        *LIST_COLUMNS, "whatsapp_number", "whatsapp_evidence", "socials",
        "address", "region", "postcode", "first_seen", "last_seen", "phone_type", "status",
        "sanctions_hit",
    )  # fmt: skip
    data = _lead_row({c: getattr(lead, c) for c in columns})
    data["source_records"] = [
        dict(r)
        for r in session.execute(
            select(LeadSource.source, LeadSource.source_record_id, LeadSource.fetched_at)
            .where(LeadSource.lead_id == lead_id)
            .order_by(LeadSource.source)
        ).mappings()
    ]
    data["outreach"] = [
        dict(r)
        for r in session.execute(
            select(
                Outreach.stage, Outreach.channel, Outreach.drafted_at, Outreach.sent_at,
                Outreach.replied_at, Outreach.bounced, Outreach.opted_out,
            ).where(Outreach.lead_id == lead_id)
        ).mappings()
    ]  # fmt: skip
    return data


def is_suppressed(session: Session, *values: str | None) -> bool:
    keys = [v.lower() for v in values if v]
    return bool(
        keys and session.scalar(select(exists().where(Suppression.email_or_phone.in_(keys))))
    )


def list_outreach(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    stmt = (
        select(
            Outreach.lead_id, Lead.name, Lead.email, Outreach.recipient, Outreach.channel,
            Outreach.stage,
            Outreach.drafted_at, Outreach.sent_at, Outreach.replied_at, Outreach.bounced,
            Outreach.opted_out, Outreach.followup_due_at,
        )
        .join(Lead, Lead.lead_id == Outreach.lead_id)
        .order_by(Outreach.drafted_at.desc().nulls_last())
        .limit(limit)
    )  # fmt: skip
    return [dict(r) for r in session.execute(stmt).mappings()]


def list_suppression(session: Session, limit: int = 500) -> list[dict[str, Any]]:
    stmt = (
        select(Suppression.email_or_phone, Suppression.reason, Suppression.added_at)
        .order_by(Suppression.added_at.desc())
        .limit(limit)
    )
    return [dict(r) for r in session.execute(stmt).mappings()]


_VISIBLE = "status = 'active' AND NOT sanctions_hit"
_STATS_TTL = 60.0
_stats_cache: tuple[float, dict[str, Any]] | None = None


def overview(session: Session) -> dict[str, Any]:
    """Headline numbers. Cached briefly: each call scans every lead."""
    global _stats_cache
    if _stats_cache and time.monotonic() - _stats_cache[0] < _STATS_TTL:
        return _stats_cache[1]

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in session.execute(text(sql)).mappings()]

    counts = """count(*) AS total,
        count(*) FILTER (WHERE NOT age_unknown) AS dated,
        count(email) AS email,
        count(phone_e164) AS phone,
        count(*) FILTER (WHERE website IS NULL) AS no_website,
        count(*) FILTER (WHERE NOT age_unknown AND email IS NOT NULL) AS dated_email,
        count(*) FILTER (WHERE NOT age_unknown AND email IS NOT NULL
                         AND website IS NULL) AS best,
        count(website_checked_at) AS checked"""
    result = {
        "totals": rows(f"SELECT {counts} FROM leads WHERE {_VISIBLE}")[0],
        "countries": rows(
            f"SELECT country_iso2 AS key, {counts} FROM leads WHERE {_VISIBLE} "
            "GROUP BY 1 ORDER BY total DESC"
        ),
        "categories": rows(
            f"SELECT category_group AS key, {counts} FROM leads WHERE {_VISIBLE} "
            "GROUP BY 1 ORDER BY total DESC"
        ),
        "years": rows(
            "SELECT extract(year FROM created_date)::int AS key, count(*) AS total "
            f"FROM leads WHERE {_VISIBLE} AND NOT age_unknown AND created_date IS NOT NULL "
            "GROUP BY 1 ORDER BY 1"
        ),
        "websites": rows(
            "SELECT coalesce(website_status, 'unsure') AS key, count(*) AS total "
            f"FROM leads WHERE {_VISIBLE} AND website_checked_at IS NOT NULL "
            "GROUP BY 1 ORDER BY 2 DESC"
        ),
        "cities": rows(
            f"SELECT initcap(lower(city)) AS key, count(*) AS total FROM leads WHERE {_VISIBLE} "
            "AND city IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ),
        "outreach": rows(
            "SELECT count(drafted_at) AS drafted, count(sent_at) AS sent, "
            "count(replied_at) AS replied, count(*) FILTER (WHERE bounced) AS bounced "
            "FROM outreach"
        )[0],
        "opted_out": rows("SELECT count(*) AS n FROM suppression")[0]["n"],
        "sources": rows(
            "SELECT source AS key, count(*) AS records, max(fetched_at) AS last_fetched "
            "FROM lead_sources GROUP BY 1 ORDER BY 2 DESC"
        ),
        "runs": [
            dict(r)
            for r in session.execute(
                select(Run.flow, Run.status, Run.started_at, Run.finished_at, Run.counts)
                .where(Run.flow.not_in(("gmail-sync", "backup-check")))
                .order_by(Run.started_at.desc())
                .limit(10)
            ).mappings()
        ],
    }
    _stats_cache = (time.monotonic(), result)
    return result


def last_runs(session: Session) -> dict[str, dict[str, Any]]:
    """Newest run of every flow."""
    stmt = (
        select(Run.flow, Run.status, Run.started_at, Run.finished_at)
        .distinct(Run.flow)
        .order_by(Run.flow, Run.started_at.desc())
    )
    return {r["flow"]: dict(r) for r in session.execute(stmt).mappings()}


def clear_cache() -> None:
    global _stats_cache
    _stats_cache = None
