"""Run the website checker on leads and store what it finds."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import ColumnElement, Select, and_, func, or_, select, update

from leads.db import session_scope
from leads.enrich.website import FetchError, WebsiteCheck, check_many, network_ok
from leads.models import Lead, LeadStatus, WebsiteStatus
from leads.pipeline.normalise import host_of, is_platform, normalise_website, registered_domain
from leads.settings import get_settings, load_filters

log = structlog.get_logger(__name__)

BATCH = 200
# A site used by many leads is a chain's: its contacts are not one lead's contacts.
MAX_LEADS_PER_SITE = 3
_FREE_MAIL = re.compile(
    r"(gmail|googlemail|outlook|hotmail|live|msn|yahoo|ymail|rocketmail|icloud|me|mac|aol|mail|"
    r"email|gmx|web|protonmail|proton|pm|zoho|yandex|btinternet|btconnect|sky|talktalk|virginmedia|"
    r"ntlworld|tiscali|eircom|onvol|go|melita|maltanet|wp|o2|qq|163|blueyonder|fsmail|vodafone)"
    r"\.(com|net|org|me|ru|de|fr|it|es|pl|ie|eu|uk|mt|co\.uk|com\.mt|net\.mt)"
)


def email_site(email: str | None) -> str | None:
    """Possible website behind a business email address (``info@acme.ie`` -> acme.ie)."""
    domain = (email or "").rpartition("@")[2].lower()
    if not domain or _FREE_MAIL.fullmatch(domain) or registered_domain(domain) is None:
        return None
    return normalise_website(domain)


def belongs_to(lead: Lead, check: WebsiteCheck) -> bool:
    """Whether a site found through an email domain shows this lead's name, phone or email."""
    text = check.text.lower()
    if lead.email and (lead.email in check.contacts.emails or lead.email in text):
        return True
    digits = re.sub(r"\D", "", lead.phone_e164 or "")
    if len(digits) >= 9 and digits[-9:] in re.sub(r"\D", "", text):
        return True
    name = re.sub(r"\W+", " ", lead.name.lower()).strip()
    return len(name) >= 6 and name in re.sub(r"\W+", " ", text)


def usable_email(check: WebsiteCheck) -> str | None:
    """An address the site shows that is its own: same domain, or a free mailbox.

    Anything else is often the web designer's or a booking service's.
    """
    site = registered_domain(host_of(check.final_url))
    for email in check.contacts.emails:
        domain = email.rpartition("@")[2]
        if registered_domain(domain) == site or _FREE_MAIL.fullmatch(domain):
            return email
    return None


def pending(limit: int | None, *, recheck: bool, cutoff: datetime) -> Select[Any]:
    """Leads to check, the ones closest to an email first."""
    due: ColumnElement[bool] = Lead.website_checked_at.is_(None)
    if recheck:
        due = or_(due, Lead.website_checked_at < cutoff)
    stmt = (
        select(Lead)
        .where(and_(due, Lead.status == LeadStatus.ACTIVE, Lead.sanctions_hit.is_(False)))
        .order_by(
            Lead.age_unknown,
            Lead.email.is_(None),
            Lead.website.is_not(None),
            Lead.created_date.desc().nulls_last(),
            Lead.lead_id,
        )
    )
    return stmt.limit(limit)


def changes(lead: Lead, check: WebsiteCheck, *, guessed: bool, shared: bool) -> dict[str, Any]:
    """Column values to store for one lead."""
    values: dict[str, Any] = {
        "website_status": check.status,
        "website_reason": check.reason,
        "website_checked_at": check.checked_at,
    }
    if check.status == WebsiteStatus.NONE:
        return values
    if guessed:
        if check.status == WebsiteStatus.PARKED or check.reason == FetchError.DNS:
            # The lead lists no website and its email domain has none either.
            values["website_status"] = WebsiteStatus.NONE
            values["website_reason"] = f"none listed; email domain: {check.reason}"
            return values
        if check.status != WebsiteStatus.WORKING:
            values["website_status"] = None
            return values
        if not belongs_to(lead, check):
            values["website_status"] = None
            values["website_reason"] = "email domain has a site, not clearly this business"
            return values
        site = check.final_url or ""
        if is_platform(site, load_filters()):
            values["website_status"] = None
            return values
        values["website"] = normalise_website(site)
        values["domain"] = registered_domain(host_of(site))
        values["website_reason"] = "found through the email address"
    if check.status != WebsiteStatus.WORKING or shared:
        return values
    if not lead.email and (email := usable_email(check)):
        values["email"] = email
    if not lead.whatsapp_number and check.contacts.whatsapp:
        hit = check.contacts.whatsapp[0]
        values["whatsapp_number"], values["whatsapp_evidence"] = hit.number, hit.evidence_url
    if check.contacts.socials:
        values["socials"] = {**check.contacts.socials, **lead.socials}
    return values


def _targets(leads: Sequence[Lead]) -> dict[str, str | None]:
    """``{lead_id: url to visit}``; None when there is nothing to visit."""
    return {
        lead.lead_id: normalise_website(lead.website) or email_site(lead.email) for lead in leads
    }


def _save(
    leads: Sequence[Lead], targets: dict[str, str | None], found: dict[str, WebsiteCheck]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    with session_scope() as session:
        domains = {d for lead in leads if (d := lead.domain)}
        crowded = set(
            session.scalars(
                select(Lead.domain)
                .where(Lead.domain.in_(domains))
                .group_by(Lead.domain)
                .having(func.count() > MAX_LEADS_PER_SITE)
            )
        )
        for lead in leads:
            url = targets[lead.lead_id]
            check = found[url] if url else WebsiteCheck(WebsiteStatus.NONE, "no website listed")
            values = changes(lead, check, guessed=not lead.website, shared=lead.domain in crowded)
            # A lead changed since it was read (e.g. a new website) is left for the next run.
            session.execute(
                update(Lead)
                .where(
                    Lead.lead_id == lead.lead_id, Lead.website.is_not_distinct_from(lead.website)
                )
                .values(**values)
            )
            key = str(values["website_status"] or "unsure")
            counts[key] = counts.get(key, 0) + 1
            for extra in ("email", "whatsapp_number", "website"):
                if extra in values:
                    counts[f"new_{extra}"] = counts.get(f"new_{extra}", 0) + 1
    return counts


def check_pending(limit: int | None = None, *, recheck: bool = False) -> dict[str, int]:
    """Check websites of leads never checked (or, with ``recheck``, checked long ago).

    Every batch is saved, so a stopped run continues where it was. A batch is thrown away
    and the run stops if this machine turns out to be offline.
    """
    settings = get_settings()
    cfg = settings.website
    cutoff = datetime.now(UTC) - timedelta(days=cfg.recheck_after_days)
    counts: dict[str, int] = {"checked": 0}
    while limit is None or counts["checked"] < limit:
        size = BATCH if limit is None else min(BATCH, limit - counts["checked"])
        with session_scope() as session:
            leads = list(session.scalars(pending(size, recheck=recheck, cutoff=cutoff)))
        if not leads:
            break
        targets = _targets(leads)
        items = {
            url: (url, url, lead.country_iso2) for lead in leads if (url := targets[lead.lead_id])
        }
        found = asyncio.run(
            check_many(list(items.values()), cfg, settings.batch.website_concurrency)
        )
        if items and not asyncio.run(network_ok(cfg)):
            log.warning("offline", message="No internet; this batch was not saved")
            counts["stopped_offline"] = 1
            break
        for key, n in _save(leads, targets, found).items():
            counts[key] = counts.get(key, 0) + n
        counts["checked"] += len(leads)
        log.info("websites_checked", **counts)
    return counts
