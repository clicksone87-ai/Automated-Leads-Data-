"""WhatsApp messages a person sends by hand from the dashboard.

Nothing here talks to WhatsApp. The dashboard opens a wa.me link with the text filled in,
and the person presses Send in their own WhatsApp Business app. Whether a number is on
WhatsApp is never checked here: WhatsApp itself says so when the chat opens (and offers
to invite the number), so no checker or unofficial API is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from leads.models import Channel, Lead, LeadStatus, Outreach, Stage, Suppression
from leads.outreach.phrases import display_name, lead_language
from leads.outreach.templates import build_context, effective_language, render
from leads.settings import Settings

AFTER_REPLY = "whatsapp_after_reply"
# For a business that shows a WhatsApp number itself and so invites contact there.
FIRST = "whatsapp_first"
# For any other phone number of the business, clicked in the dashboard.
PHONE = "whatsapp_phone"
NO_LINK = "[add the mock-up link]"


class WhatsAppError(Exception):
    pass


@dataclass(slots=True)
class WhatsAppState:
    number: str | None = None
    text: str = ""
    language: str = "en"
    replied: bool = False
    public: bool = False  # the business shows this number for WhatsApp itself
    opened_at: datetime | None = None
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _replied(session: Session, lead_id: str) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    (Outreach.lead_id == lead_id)
                    & (Outreach.channel == Channel.EMAIL)
                    & Outreach.replied_at.is_not(None)
                    & ~Outreach.opted_out
                )
            )
        )
    )


def _number(lead: Lead) -> str | None:
    """The number shown for WhatsApp, else the business's own phone number."""
    return lead.whatsapp_number or lead.phone_e164 or None


def state(session: Session, lead: Lead, settings: Settings) -> WhatsAppState:
    """Whether this lead may get a WhatsApp message now, and the suggested text."""
    replied = _replied(session, lead.lead_id)
    result = WhatsAppState(replied=replied, number=_number(lead), public=bool(lead.whatsapp_number))
    result.opened_at = session.scalar(
        select(Outreach.drafted_at).where(
            (Outreach.lead_id == lead.lead_id) & (Outreach.channel == Channel.WHATSAPP)
        )
    )
    if lead.status != LeadStatus.ACTIVE:
        result.blockers.append("This business is closed.")
    if lead.sanctions_hit:
        result.blockers.append("This business matches a sanctions list.")
    keys = [
        k.lower() for k in (lead.email, lead.phone_e164, lead.whatsapp_number, result.number) if k
    ]
    if keys and session.scalar(select(exists().where(Suppression.email_or_phone.in_(keys)))):
        result.blockers.append("This contact has opted out.")
    if result.number is None:
        result.blockers.append("There is no phone or WhatsApp number for this business.")
    elif result.opened_at is None and session.scalar(
        select(
            exists().where(
                (Outreach.channel == Channel.WHATSAPP) & (Outreach.recipient == result.number)
            )
        )
    ):
        result.blockers.append("This number was already messaged for another lead.")
    if result.blockers:
        return result

    language = lead_language(lead.name, lead.country_iso2)
    template = AFTER_REPLY if replied else FIRST if lead.whatsapp_number else PHONE
    result.language = effective_language(template, language)
    context = build_context(
        business_name=display_name(lead.name, result.language),
        category=lead.category,
        city=lead.city,
        website_status=lead.website_status,
        domain=lead.domain,
        sender=settings.sender,
        delivery_days=settings.outreach.delivery_days,
        price=settings.outreach.price,
        language=result.language,
        link=settings.sender.mockup_link or NO_LINK,
    )
    result.text = render(template, context, result.language).body.strip()
    if NO_LINK in result.text:
        result.warnings.append("Add the mock-up link to the message before sending.")
    if settings.sender.sender_name in ("", "Your Name") or not settings.sender.company:
        result.warnings.append("The sender name or company is not filled in yet.")
    return result


def record_opened(session: Session, lead: Lead, settings: Settings) -> datetime:
    """Remember that the person opened WhatsApp for this lead; repeats keep the first time."""
    current = state(session, lead, settings)
    if current.blockers:
        raise WhatsAppError(current.blockers[0])
    if current.opened_at is not None:
        return current.opened_at
    now = datetime.now(UTC)
    session.add(
        Outreach(
            lead_id=lead.lead_id,
            channel=Channel.WHATSAPP,
            stage=Stage.REPLIED if current.replied else Stage.FIRST,
            recipient=current.number,
            drafted_at=now,
        )
    )
    session.commit()
    return now
