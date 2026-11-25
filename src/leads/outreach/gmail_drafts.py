"""Daily batch of personalised Gmail drafts (users.drafts.create) with segment labels.

The user reviews and sends from Gmail. This module never calls messages.send.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

import structlog
from sqlalchemy import Select, and_, case, exists, false, select

from leads.models import Lead, LeadStatus, Outreach, Suppression, WebsiteStatus
from leads.outreach.gmail_api import execute

log = structlog.get_logger(__name__)

# Lower = drafted first. Brief: none > broken > parked > working. A social-only "website"
# is treated like having no website.
DRAFT_PRIORITY: dict[str, int] = {
    WebsiteStatus.NONE: 0,
    WebsiteStatus.REDIRECT_SOCIAL: 1,
    WebsiteStatus.BROKEN: 2,
    WebsiteStatus.PARKED: 3,
    WebsiteStatus.WORKING: 4,
}


def draft_priority(website_status: str | None) -> int:
    return DRAFT_PRIORITY.get(website_status or "", 99)


def candidates_query(limit: int, *, include_unknown_age: bool) -> Select[Lead]:
    """Leads eligible for a first-email draft, highest priority first.

    Eligible: active, has an email, website checked, not a sanctions hit, no outreach yet,
    email not suppressed, and (age known or include_unknown_age).
    """
    priority = case(
        *((Lead.website_status == status, rank) for status, rank in DRAFT_PRIORITY.items()),
        else_=99,
    )
    conditions = [
        Lead.status == LeadStatus.ACTIVE,
        Lead.email.is_not(None),
        Lead.website_status.is_not(None),
        Lead.sanctions_hit.is_(false()),
        ~exists().where(Outreach.lead_id == Lead.lead_id),
        ~exists().where(Suppression.email_or_phone == Lead.email),
    ]
    if not include_unknown_age:
        conditions.append(Lead.age_unknown.is_(false()))
    return (
        select(Lead)
        .where(and_(*conditions))
        .order_by(priority, Lead.created_date.desc().nulls_last(), Lead.lead_id)
        .limit(limit)
    )


def build_mime(to: str, subject: str, body: str) -> EmailMessage:
    """Plain-text message. From is set by Gmail to the signed-in account."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def encode_message(msg: EmailMessage) -> dict[str, str]:
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}


def create_draft(
    service: Any, msg: EmailMessage, label_ids: list[str], user_id: str = "me"
) -> dict[str, Any]:
    """Create a Gmail draft and apply labels to its message. Returns the draft resource."""
    draft: dict[str, Any] = execute(
        service.users().drafts().create(userId=user_id, body={"message": encode_message(msg)}),
        idempotent=False,
    )
    if label_ids:
        # The draft exists by now: a label that cannot be set is added by the sync later.
        try:
            execute(
                service.users()
                .messages()
                .modify(userId=user_id, id=draft["message"]["id"], body={"addLabelIds": label_ids})
            )
        except Exception as exc:
            log.warning("labels_not_set", error=repr(exc))
    return draft


def create_daily_drafts(batch_size: int | None = None, *, dry_run: bool = False) -> dict[str, int]:
    """Write and save drafts for the best leads, up to the daily limit.

    ``dry_run`` writes the emails and prints them, without touching Gmail.
    """
    from leads.db import session_scope
    from leads.outreach import drafts
    from leads.outreach.ai import AiError
    from leads.settings import get_settings

    settings = get_settings()
    counts = {"drafted": 0, "skipped": 0}
    with session_scope() as session:
        room = settings.batch.drafts_per_day - drafts.drafted_today(session)
        wanted = min(room, batch_size if batch_size is not None else room)
        if wanted <= 0:
            return counts
        # Some candidates are skipped, e.g. two leads that share an address.
        query = candidates_query(wanted * 3, include_unknown_age=settings.include_unknown_age)
        lead_ids = [lead.lead_id for lead in session.scalars(query)]
    for lead_id in lead_ids:
        if counts["drafted"] >= wanted:
            break
        try:
            email = drafts.generate(lead_id, settings=settings)
            if dry_run:
                print(f"--- {lead_id}\nSubject: {email.subject}\n\n{email.body}")
            else:
                drafts.save_to_gmail(lead_id, email.subject, email.body, settings=settings)
        except AiError as exc:
            # A refused text is one lead's problem; a missing key or used-up limit is everyone's.
            counts["skipped"] += 1
            log.warning("email_not_written", lead_id=lead_id, reason=str(exc))
            if "safe email" not in str(exc):
                break
            continue
        except drafts.DraftError as exc:
            counts["skipped"] += 1
            log.warning("draft_skipped", lead_id=lead_id, reason=str(exc))
            if "Gmail" in str(exc) or "limit" in str(exc):
                break
            continue
        counts["drafted"] += 1
    return counts
