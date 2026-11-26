"""Run the website checker on leads and store what it finds."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import structlog
from sqlalchemy import ColumnElement, Select, and_, func, or_, select, update

from leads import monitoring
from leads.db import lock_leads, session_scope
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


# Profile links whose user name can also be a domain name ("instagram.com/acmecafe").
_HANDLE = re.compile(
    r"^https?://(?:www\.|m\.|mobile\.)?(?:instagram\.com|x\.com|twitter\.com|tiktok\.com"
    r"|youtube\.com|pinterest\.com)/@?([a-z0-9._-]{4,40})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_NOT_HANDLES = {
    "explore", "share", "channel", "user", "watch", "intent", "home", "hashtag", "search",
    "reel", "reels", "stories", "pages", "profile", "login", "accounts",
}  # fmt: skip


def social_site(socials: dict[str, Any] | None) -> str | None:
    """Website named like a social profile (instagram.com/acme.cafe -> acmecafe.com).

    Only a guess: :func:`changes` keeps it only if the site links back to that profile or
    shows the lead's phone or email.
    """
    for url in (socials or {}).values():
        match = _HANDLE.match(url) if isinstance(url, str) else None
        if not match or match.group(1).lower() in _NOT_HANDLES:
            continue
        label = re.sub(r"[._]", "", match.group(1).lower()).strip("-")
        if len(label) >= 4 and not label.isdigit():
            return normalise_website(f"{label}.com")
    return None


def _profile(url: str) -> str:
    """Comparable form of a profile link: host without www, path without slashes or @."""
    parts = urlsplit(url.strip())
    host = re.sub(r"^(www|m|mobile)\.", "", parts.hostname or "").replace("twitter.com", "x.com")
    return host + "/" + parts.path.strip("/").lstrip("@").lower()


def links_back(lead: Lead, check: WebsiteCheck) -> bool:
    """Whether the site links to one of the lead's own social profiles."""
    own = {_profile(u) for u in (lead.socials or {}).values() if isinstance(u, str)}
    return any(_profile(u) in own for u in check.contacts.socials.values())


def shows_contact(lead: Lead, check: WebsiteCheck) -> bool:
    """Whether the site shows this lead's email or phone number."""
    text = check.text.lower()
    if lead.email and (lead.email in check.contacts.emails or lead.email in text):
        return True
    digits = re.sub(r"\D", "", lead.phone_e164 or "")
    return len(digits) >= 9 and digits[-9:] in re.sub(r"\D", "", text)


def belongs_to(lead: Lead, check: WebsiteCheck) -> bool:
    """Whether a site found through an email domain shows this lead's name, phone or email."""
    if shows_contact(lead, check):
        return True
    name = re.sub(r"\W+", " ", lead.name.lower()).strip()
    return len(name) >= 6 and name in re.sub(r"\W+", " ", check.text.lower())


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


def changes(
    lead: Lead, check: WebsiteCheck, *, guessed: bool, shared: bool, via: str = "email"
) -> dict[str, Any]:
    """Column values to store for one lead.

    ``via`` says where a guessed site came from: ``email`` (its domain) or ``social``
    (a profile user name).
    """
    values: dict[str, Any] = {
        "website_status": check.status,
        "website_reason": check.reason,
        "website_checked_at": check.checked_at,
    }
    if check.status == WebsiteStatus.NONE:
        return values
    if guessed:
        if check.status == WebsiteStatus.PARKED or check.reason == FetchError.DNS:
            # The lead lists no website and the guessed domain has none either.
            values["website_status"] = WebsiteStatus.NONE
            values["website_reason"] = (
                f"none listed; email domain: {check.reason}"
                if via == "email"
                else "no website listed"
            )
            return values
        if check.status != WebsiteStatus.WORKING:
            # A failing .com of a profile name says nothing about this business.
            values["website_status"] = None if via == "email" else WebsiteStatus.NONE
            if via == "social":
                values["website_reason"] = "no website listed"
            return values
        # A name match is enough for the business's own email domain, but anyone can own
        # the .com of a profile name, so that needs a link back or the same contact details.
        if via == "social" and not (links_back(lead, check) or shows_contact(lead, check)):
            values["website_status"] = WebsiteStatus.NONE
            values["website_reason"] = "no website listed"
            return values
        if via == "email" and not belongs_to(lead, check):
            values["website_status"] = None
            values["website_reason"] = "email domain has a site, not clearly this business"
            return values
        site = check.final_url or ""
        if is_platform(site, load_filters()):
            values["website_status"] = None
            return values
        values["website"] = normalise_website(site)
        values["domain"] = registered_domain(host_of(site))
        values["website_reason"] = (
            "found through the email address"
            if via == "email"
            else "found through a social profile name"
        )
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


def _targets(leads: Sequence[Lead]) -> dict[str, tuple[str | None, str]]:
    """``{lead_id: (url to visit, where it came from)}``; url None when there is none."""
    found: dict[str, tuple[str | None, str]] = {}
    for lead in leads:
        if listed := normalise_website(lead.website):
            found[lead.lead_id] = (listed, "listed")
        elif guess := email_site(lead.email):
            found[lead.lead_id] = (guess, "email")
        else:
            found[lead.lead_id] = (social_site(lead.socials), "social")
    return found


def _save(
    leads: Sequence[Lead],
    targets: dict[str, tuple[str | None, str]],
    found: dict[str, WebsiteCheck],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    with session_scope() as session:
        lock_leads(session)
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
            url, via = targets[lead.lead_id]
            check = found[url] if url else WebsiteCheck(WebsiteStatus.NONE, "no website listed")
            values = changes(
                lead, check, guessed=not lead.website, shared=lead.domain in crowded, via=via
            )
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


def watch(pause_seconds: float = 300.0) -> None:
    """Check websites without end: new leads first, then those due again.

    Waits when the internet is down or nothing is due, and survives a database restart.
    """
    while True:
        try:
            counts = check_pending(BATCH * 10, recheck=True)
        except Exception as exc:
            log.warning("website_watch_failed", error=repr(exc))
            monitoring.report(exc)
            counts = {"checked": 0}
        if counts["checked"] == 0 or "stopped_offline" in counts:
            time.sleep(pause_seconds)


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
            url: (url, url, lead.country_iso2)
            for lead in leads
            if (url := targets[lead.lead_id][0])
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
