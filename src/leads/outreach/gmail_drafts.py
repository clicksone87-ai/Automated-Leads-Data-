"""Daily batch of personalised Gmail drafts (users.drafts.create) with segment labels.

The user reviews and sends from Gmail. This module never calls messages.send.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from sqlalchemy import Select, and_, case, exists, false, select

from leads.models import Lead, LeadStatus, Outreach, Suppression, WebsiteStatus

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
    draft: dict[str, Any] = (
        service.users()
        .drafts()
        .create(userId=user_id, body={"message": encode_message(msg)})
        .execute()
    )
    if label_ids:
        service.users().messages().modify(
            userId=user_id, id=draft["message"]["id"], body={"addLabelIds": label_ids}
        ).execute()
    return draft


def create_daily_drafts(batch_size: int | None = None, *, dry_run: bool = False) -> dict[str, int]:
    """Create today's batch of first-email drafts.

    TODO(build step 7):
      1. ``settings.batch.drafts_per_day`` unless ``batch_size`` is given.
      2. Run ``candidates_query`` in a session.
      3. For each lead: language = ``templates.language_for_country``; render
         ``email_first`` with ``templates.build_context``; ``build_mime``.
      4. ``labels.labels_for_lead(stage=Stage.FIRST, category_group=<display name>)``
         -> ``labels.ensure_labels`` once per batch.
      5. Unless ``dry_run``: ``create_draft``; insert ``Outreach(stage='first',
         gmail_draft_id, gmail_thread_id, drafted_at=now)``. Commit per lead so a crash
         never produces duplicate drafts.
      6. ``dry_run``: print subject/recipient instead, no Gmail calls, no DB writes.
      7. Return {"drafted": n, "skipped": n}.
    """
    raise NotImplementedError("gmail_drafts.create_daily_drafts (build step 7)")
