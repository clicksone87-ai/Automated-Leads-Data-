"""One first-email Gmail draft per lead and per address. Never sends."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any

import dns.exception
import dns.resolver
import structlog
from sqlalchemy import exists, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leads.db import session_scope
from leads.models import (
    GeneratedEmail,
    Lead,
    LeadStatus,
    Outreach,
    Stage,
    Suppression,
)
from leads.outreach import ai
from leads.outreach.gmail_api import execute
from leads.outreach.gmail_drafts import build_mime, create_draft
from leads.outreach.labels import ensure_labels, labels_for_lead
from leads.outreach.phrases import LANGUAGES, display_name, lead_language, readable
from leads.pipeline.sanctions import current_screen
from leads.settings import Settings, get_settings, load_categories

log = structlog.get_logger(__name__)

_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+")
_NO_REPLY = re.compile(r"^(?:no-?reply|do-?not-?reply|postmaster|mailer-daemon|abuse)\b")


class DraftError(RuntimeError):
    """Why no draft was made, in words the user can act on."""


def accepts_mail(domain: str, timeout: float = 8.0) -> bool | None:
    """Whether a domain can receive email. None = the lookup itself failed."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    for rtype in ("MX", "A", "AAAA"):
        try:
            answer = resolver.resolve(domain, rtype)
        except dns.resolver.NXDOMAIN:
            return False
        except dns.resolver.NoAnswer:
            continue
        except dns.exception.DNSException:
            return None
        # A lone "." is a domain saying it takes no mail.
        return not (rtype == "MX" and all(str(r.exchange) == "." for r in answer))
    return False


def blockers(session: Session, lead: Lead, settings: Settings) -> list[str]:
    """Everything that stops a draft for this lead, without network calls."""
    found: list[str] = []
    email = (lead.email or "").strip().lower()
    if not email:
        found.append("This lead has no email address.")
    elif not _EMAIL.fullmatch(email) or _NO_REPLY.match(email):
        found.append("This email address cannot be used.")
    if lead.status != LeadStatus.ACTIVE:
        found.append("This business is closed.")
    if lead.sanctions_hit:
        found.append("This business matches a sanctions list.")
    keys = [k for k in (email, lead.phone_e164, email.rpartition("@")[2]) if k]
    if keys and session.scalar(select(exists().where(Suppression.email_or_phone.in_(keys)))):
        found.append("This contact has opted out.")
    if session.scalar(
        select(
            exists().where(
                (Outreach.lead_id == lead.lead_id)
                | ((Outreach.recipient == email) & (Outreach.recipient != ""))
            )
        )
    ):
        found.append("A draft already exists for this lead or this address.")
    if not lead.website_status:
        found.append(
            "The website check could not tell if the site works."
            if lead.website_checked_at
            else "The website has not been checked yet."
        )
    if lead_language(lead.name, lead.country_iso2) not in LANGUAGES:
        found.append("Emails in this country's language are not ready yet.")
    if settings.sender.company in ("", "Your Company"):
        found.append("The company name is not filled in.")
    return found


def warnings(settings: Settings) -> list[str]:
    """Missing sender details: a draft can be made, but should not be sent like this."""
    sender = settings.sender
    missing = [
        label
        for label, value in (
            ("sender name", sender.sender_name if sender.sender_name != "Your Name" else ""),
            ("phone", sender.company_phone),
            ("website", sender.company_website),
        )
        if not value
    ]
    found = [f"The signature has no {', '.join(missing)}."] if missing else []
    if not re.search(r"\d", sender.company_address):
        found.append("The signature has only a city; a full postal address is needed.")
    return found


def facts_for(lead: Lead) -> ai.LeadFacts:
    category = lead.category
    if category:
        category = category.split("|")[0].split(">")[-1].strip()
        if re.match(r"\d", category):
            category = None
    language = lead_language(lead.name, lead.country_iso2)
    return ai.LeadFacts(
        name=display_name(lead.name, language),
        category=category or load_categories().display_name(lead.category_group),
        city=readable(lead.city, language),
        website_status=lead.website_status or "",
        website_reason=lead.website_reason,
        domain=lead.domain,
        language=language,
    )


def saved_email(session: Session, lead: Lead) -> GeneratedEmail | None:
    """The stored text, unless the website has changed since it was written."""
    row = session.get(GeneratedEmail, lead.lead_id)
    return row if row and row.website_status == lead.website_status else None


def generate(lead_id: str, *, again: bool = False, settings: Settings | None = None) -> ai.Email:
    """Write (or return the stored) email for a lead."""
    settings = settings or get_settings()
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise DraftError("Lead not found")
        if found := blockers(session, lead, settings):
            raise DraftError(" ".join(found))
        saved = saved_email(session, lead)
        if saved and not again:
            return ai.Email(saved.subject, saved.body, saved.model)
        facts = facts_for(lead)
    email = ai.write_email(facts, settings)
    with session_scope() as session:
        session.merge(
            GeneratedEmail(
                lead_id=lead_id,
                subject=email.subject,
                body=email.body,
                model=email.model,
                website_status=facts.website_status,
                created_at=datetime.now(UTC),
            )
        )
    return email


def drafted_today(session: Session) -> int:
    start = datetime.combine(datetime.now(UTC).date(), time.min, UTC)
    stmt = select(func.count()).select_from(Outreach).where(Outreach.drafted_at >= start)
    return session.scalar(stmt) or 0


def check_text(subject: str, body: str) -> tuple[str, str]:
    """Edited text, kept honest: a subject, a body, and a way to opt out."""
    subject = re.sub(r"\s+", " ", subject).strip()
    body = body.replace("\r\n", "\n").strip() + "\n"
    if not 3 <= len(subject) <= 200:
        raise DraftError("The subject must be between 3 and 200 characters.")
    if not 40 <= len(body) <= 8000:
        raise DraftError("The email text is too short or too long.")
    if not any(line in body for line in ai.OPT_OUTS):
        raise DraftError("Keep the last line that says how to opt out.")
    return subject, body


def basis_note(lead: Lead) -> str:
    """Where the address came from, kept for questions about why we wrote."""
    seen = f", first seen {lead.first_seen:%Y-%m-%d}" if lead.first_seen else ""
    origin = ", ".join(lead.sources) or "unknown source"
    return (
        f"Business email published by {origin}{seen}. One-to-one business offer with an "
        "opt-out line. Local rules not yet reviewed by a lawyer."
    )


def check_account(service: Any, settings: Settings) -> None:
    if not settings.gmail_account:
        raise DraftError("GMAIL_ACCOUNT is missing in .env")
    profile = execute(service.users().getProfile(userId=settings.gmail_user_id))
    actual = str(profile.get("emailAddress", "")).lower()
    if actual != settings.gmail_account.strip().lower():
        raise DraftError("Gmail is signed in to a different account than GMAIL_ACCOUNT in .env")


def _gmail(settings: Settings) -> Any:
    from leads.google_auth import gmail_service

    return gmail_service(settings)


def save_to_gmail(
    lead_id: str,
    subject: str,
    body: str,
    *,
    settings: Settings | None = None,
    service_factory: Callable[[Settings], Any] | None = None,
) -> str:
    """Create the Gmail draft for a lead. Returns the draft id.

    The outreach row is stored before Gmail is called, so a crash or a double click can
    never lead to two drafts for one lead or one address.
    """
    settings = settings or get_settings()
    subject, body = check_text(subject, body)
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise DraftError("Lead not found")
        if found := blockers(session, lead, settings):
            raise DraftError(" ".join(found))
        recipient = str(lead.email).strip().lower()
        name, country = lead.name, lead.country_iso2
        basis = basis_note(lead)
        labels = labels_for_lead(
            country_iso2=country,
            category_group=load_categories().display_name(lead.category_group),
            website_status=lead.website_status,
            stage=Stage.FIRST,
        )
    if current_screen(settings).match(name, country):
        with session_scope() as session:
            session.execute(update(Lead).where(Lead.lead_id == lead_id).values(sanctions_hit=True))
        raise DraftError("This business matches a sanctions list.")
    mail_ok = accepts_mail(recipient.rpartition("@")[2])
    if mail_ok is False:
        raise DraftError("This email address cannot receive mail; its domain does not exist.")
    if mail_ok is None:
        raise DraftError("Could not check the email address. Check the internet and try again.")

    try:
        service = (service_factory or _gmail)(settings)
        check_account(service, settings)
        ids = ensure_labels(service, labels, settings.gmail_user_id)
    except DraftError:
        raise
    except Exception as exc:
        log.warning("gmail_unavailable", error=repr(exc))
        raise DraftError("Gmail is not connected. Sign in to Google again.") from exc

    try:
        with session_scope() as session:
            # Serialises the daily limit across parallel clicks.
            session.execute(text("SELECT pg_advisory_xact_lock(748301)"))
            if drafted_today(session) >= settings.batch.drafts_per_day:
                raise DraftError("Today's limit of drafts is reached.")
            row = Outreach(
                lead_id=lead_id,
                channel="email",
                stage=Stage.FIRST,
                recipient=recipient,
                drafted_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            row_id = row.id
    except IntegrityError as exc:
        raise DraftError("A draft already exists for this lead or this address.") from exc

    try:
        draft = create_draft(
            service, build_mime(recipient, subject, body), list(ids.values()),
            settings.gmail_user_id,
        )  # fmt: skip
    except Exception as exc:
        _release(row_id, exc)
        raise DraftError(
            "Gmail did not confirm the draft. Check the Drafts folder before trying again."
        ) from exc
    with session_scope() as session:
        session.execute(
            update(Lead)
            .where(Lead.lead_id == lead_id, Lead.lawful_basis_note.is_(None))
            .values(lawful_basis_note=basis)
        )
        session.execute(
            update(Outreach)
            .where(Outreach.id == row_id)
            .values(
                gmail_draft_id=draft["id"],
                gmail_thread_id=draft.get("message", {}).get("threadId"),
            )
        )
    return str(draft["id"])


def _release(row_id: int, exc: Exception) -> None:
    """Free the lead again only when Gmail clearly refused; after a lost reply it may exist."""
    from googleapiclient.errors import HttpError

    log.warning("draft_failed", error=repr(exc))
    if isinstance(exc, HttpError) and 400 <= int(exc.resp.status) < 500:
        with session_scope() as session:
            if row := session.get(Outreach, row_id):
                session.delete(row)
