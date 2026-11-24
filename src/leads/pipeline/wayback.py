"""Wayback Machine age evidence for leads that have a website but no known age."""

from __future__ import annotations

import time
from collections.abc import Collection
from datetime import date
from itertools import batched
from typing import Any

import httpx
import structlog
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leads.db import session_scope
from leads.models import DomainAge, Lead, LeadStatus, Outreach
from leads.pipeline.age import wayback_first_capture
from leads.pipeline.normalise import SOCIAL_DOMAINS
from leads.settings import get_settings

log = structlog.get_logger(__name__)

MAX_FAILURES_IN_A_ROW = 5
# A domain used by many leads belongs to a chain or platform, not to one business.
MAX_LEADS_PER_DOMAIN = 8


def load_first_captures(
    session: Session, domains: Collection[str], chunk: int = 5000
) -> dict[str, date]:
    """Known first captures for ``domains`` (never-archived domains are left out)."""
    found: dict[str, date] = {}
    for part in batched(sorted(domains), chunk):
        stmt = select(DomainAge.domain, DomainAge.first_capture).where(
            DomainAge.domain.in_(part), DomainAge.first_capture.is_not(None)
        )
        found.update({domain: captured for domain, captured in session.execute(stmt) if captured})
    return found


def pending_domains(session: Session, limit: int) -> list[str]:
    """Domains not asked about yet; those of leads with an email come first."""
    stmt = (
        select(Lead.domain)
        .where(
            Lead.age_unknown.is_(True),
            Lead.status == LeadStatus.ACTIVE,
            Lead.domain.is_not(None),
            Lead.domain.not_in(SOCIAL_DOMAINS),
            ~exists().where(DomainAge.domain == Lead.domain),
        )
        .group_by(Lead.domain)
        .having(func.count() <= MAX_LEADS_PER_DOMAIN)
        .order_by(func.count(Lead.email).desc(), Lead.domain)
        .limit(limit)
    )
    return [domain for domain in session.scalars(stmt) if domain]


def apply_capture(session: Session, domain: str, captured: date, since: date) -> str:
    """Date the unknown-age leads of ``domain``; remove or close those that are too old."""
    unknown = (Lead.domain == domain, Lead.age_unknown.is_(True))
    if captured >= since:
        session.execute(
            update(Lead)
            .where(*unknown)
            .values(created_date=captured, age_source="wayback", age_unknown=False)
        )
        return "dated"
    contacted = exists().where(Outreach.lead_id == Lead.lead_id)
    session.execute(
        delete(Lead).where(*unknown, ~contacted).execution_options(synchronize_session=False)
    )
    session.execute(
        update(Lead)
        .where(*unknown)
        .values(
            created_date=captured,
            age_source="wayback",
            age_unknown=False,
            status=LeadStatus.CLOSED.value,
        )
    )
    return "too_old"


def check_wayback(limit: int | None = None) -> dict[str, int]:
    """Look up pending domains, slowly, and apply what is found.

    Failures are not stored, so they are retried on the next run. Stops early when
    archive.org keeps failing.
    """
    settings = get_settings()
    counts: dict[str, int] = {
        "checked": 0,
        "dated": 0,
        "too_old": 0,
        "not_archived": 0,
        "failed": 0,
    }
    if not settings.wayback.enabled:
        return counts
    with session_scope() as session:
        domains = pending_domains(session, settings.wayback.daily_limit if limit is None else limit)
    failures = 0
    headers: dict[str, Any] = {"User-Agent": settings.website.user_agent}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for domain in domains:
            started = time.monotonic()
            try:
                captured = wayback_first_capture(domain, client=client, timeout=30.0)
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                counts["failed"] += 1
                log.warning("wayback_failed", domain=domain, error=str(exc) or type(exc).__name__)
                if failures >= MAX_FAILURES_IN_A_ROW:
                    break
                time.sleep(settings.wayback.seconds_per_request * 2**failures)
                continue
            failures = 0
            counts["checked"] += 1
            with session_scope() as session:
                session.execute(
                    insert(DomainAge)
                    .values(domain=domain, first_capture=captured)
                    .on_conflict_do_update(
                        index_elements=["domain"],
                        set_={"first_capture": captured, "checked_at": func.now()},
                    )
                )
                outcome = (
                    apply_capture(session, domain, captured, settings.since)
                    if captured
                    else "not_archived"
                )
            counts[outcome] += 1
            time.sleep(
                max(0.0, settings.wayback.seconds_per_request - (time.monotonic() - started))
            )
    return counts
