"""Gmail sync (every 30 min): detect sent / replied / bounced / opted out, re-apply labels
to the sent thread, and create ONE follow-up draft 6 days after a sent email with no reply.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leads.db import session_scope
from leads.models import Lead, LeadStatus, Outreach, Stage, Suppression
from leads.outreach import ai
from leads.outreach.drafts import check_account
from leads.outreach.gmail_api import execute
from leads.outreach.gmail_drafts import build_mime, encode_message
from leads.outreach.labels import ensure_labels, labels_for_lead, other_stage_labels
from leads.outreach.phrases import LANGUAGES, display_name, lead_language
from leads.outreach.templates import build_context, render
from leads.settings import Settings, get_settings, load_categories

log = structlog.get_logger(__name__)

WATCH_DAYS = 60  # how long after sending a thread is still looked at

# Reply text containing any of these (any language) adds the sender to suppression.
OPT_OUT_PHRASES: dict[str, tuple[str, ...]] = {
    "en": ("no thanks", "no thank you", "unsubscribe", "stop", "remove", "not interested",
           "don't contact", "do not contact"),
    "fr": ("non merci", "désabonner", "désinscrire", "désinscription", "retirer", "supprimer",
           "arrêtez", "pas intéressé"),
    "de": ("nein danke", "abmelden", "abbestellen", "stopp", "entfernen", "kein interesse"),
    "es": ("no gracias", "darse de baja", "dar de baja", "baja", "eliminar", "quitar",
           "no me interesa", "no estoy interesado"),
    "it": ("no grazie", "disiscrivi", "disiscrivermi", "cancellami", "rimuovi", "rimuovere",
           "non interessato"),
    "pt": ("não obrigado", "não, obrigado", "não obrigada", "descadastrar", "remover", "parar",
           "sem interesse", "não tenho interesse"),
    "nl": ("nee bedankt", "afmelden", "uitschrijven", "verwijder", "verwijderen",
           "geen interesse"),
    "pl": ("nie dziękuję", "nie, dziękuję", "wypisz", "wypisać", "usuń",
           "nie jestem zainteresowany"),
    "ar": ("لا شكرا", "لا شكراً", "إلغاء الاشتراك", "الغاء الاشتراك", "توقف", "احذف", "غير مهتم"),
    "ru": ("нет спасибо", "нет, спасибо", "отписаться", "отпишите", "стоп", "удалить", "удалите",
           "не интересно"),
    "tr": ("hayır teşekkürler", "abonelikten çık", "dur", "kaldır", "listeden çıkar",
           "ilgilenmiyorum"),
}  # fmt: skip

_ALL_PHRASES = sorted(
    {p.casefold() for ps in OPT_OUT_PHRASES.values() for p in ps}, key=len, reverse=True
)
_OPT_OUT_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(p) for p in _ALL_PHRASES) + r")(?!\w)", re.UNICODE
)

_QUOTE_HEADER_RE = re.compile(
    r"^(?:On .+ wrote:|Le .+ a écrit\s?:|Am .+ schrieb .+:|El .+ escribió:"
    r"|-----Original Message-----|From: .+)$",
    re.IGNORECASE | re.MULTILINE,
)

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
_HARD_BOUNCE_RE = re.compile(
    r"\b5\.[0-7]\.\d{1,3}\b|address not found|user unknown|no such user|does not exist"
    r"|mailbox unavailable|recipient address rejected|undeliverable",
    re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """Only the new part of a reply: cut at the quote header and drop ``>`` lines."""
    match = _QUOTE_HEADER_RE.search(text)
    if match:
        text = text[: match.start()]
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def is_opt_out(reply_text: str) -> bool:
    """True if the reply (excluding quoted text) asks not to be contacted.

    Deliberately broad ("stop", "remove"): a false positive only means no more drafts.
    """
    return bool(_OPT_OUT_RE.search(strip_quoted(reply_text).casefold()))


def is_hard_bounce(from_address: str, subject: str, body: str) -> bool:
    sender = from_address.lower()
    if not any(s in sender for s in BOUNCE_SENDERS):
        return False
    return bool(_HARD_BOUNCE_RE.search(f"{subject}\n{body}"))


def followup_due_at(sent_at: datetime, days: int = 6) -> datetime:
    return sent_at + timedelta(days=days)


@dataclass(slots=True)
class ThreadState:
    """What a Gmail thread says about one outreach message."""

    sent_at: datetime | None = None
    sent_message_id: str | None = None  # RFC 822 Message-ID of the sent email
    subject: str = ""
    replied_at: datetime | None = None
    opted_out: bool = False
    bounced: bool = False


def header(message: dict[str, Any], name: str) -> str:
    for item in message.get("payload", {}).get("headers", []):
        if str(item.get("name", "")).lower() == name.lower():
            return str(item.get("value", ""))
    return ""


def body_text(message: dict[str, Any]) -> str:
    """Plain text of a message (HTML stripped when there is nothing else)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        data = part.get("body", {}).get("data")
        if data:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            if part.get("mimeType") == "text/plain" or str(part.get("mimeType", "")).startswith(
                "message/"
            ):
                plain.append(raw)
            elif part.get("mimeType") == "text/html":
                html.append(raw)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(message.get("payload", {}))
    if plain:
        return "\n".join(plain)
    if html:
        tree = LexborHTMLParser("\n".join(html))
        for quoted in tree.css("blockquote, .gmail_quote"):
            quoted.decompose()
        return tree.body.text(separator="\n") if tree.body else ""
    return str(message.get("snippet", ""))


def read_thread(messages: list[dict[str, Any]]) -> ThreadState:
    """Sent / replied / bounced / opted out, from the messages of one thread."""
    state = ThreadState()
    for message in sorted(messages, key=lambda m: int(m.get("internalDate", 0))):
        labels = set(message.get("labelIds", []))
        at = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000, UTC)
        if "DRAFT" in labels:
            continue
        if "SENT" in labels:
            if state.sent_at is None:
                state.sent_at = at
            state.sent_message_id = header(message, "Message-ID") or state.sent_message_id
            state.subject = state.subject or header(message, "Subject")
            continue
        if state.sent_at is None:
            continue
        sender, subject, text = header(message, "From"), header(message, "Subject"), ""
        text = body_text(message)
        if any(s in sender.lower() for s in BOUNCE_SENDERS):
            state.bounced = state.bounced or is_hard_bounce(sender, subject, text)
            continue
        # An out-of-office note is neither an answer nor an opt-out.
        if header(message, "Auto-Submitted").lower() not in ("", "no"):
            continue
        state.replied_at = state.replied_at or at
        state.opted_out = state.opted_out or is_opt_out(text)
    return state


def _missing(exc: Exception) -> bool:
    from googleapiclient.errors import HttpError

    return isinstance(exc, HttpError) and int(exc.resp.status) == 404


def _draft_exists(service: Any, user: str, draft_id: str) -> bool:
    try:
        execute(service.users().drafts().get(userId=user, id=draft_id, format="minimal"))
    except Exception as exc:
        if _missing(exc):
            return False
        raise
    return True


def _thread(service: Any, user: str, thread_id: str) -> list[dict[str, Any]]:
    try:
        found = execute(service.users().threads().get(userId=user, id=thread_id, format="full"))
    except Exception as exc:
        if _missing(exc):
            return []
        raise
    return list(found.get("messages", []))


def suppress(session: Session, contact: str, reason: str) -> None:
    session.execute(
        insert(Suppression)
        .values(email_or_phone=contact.lower(), reason=reason)
        .on_conflict_do_nothing()
    )


def _relabel(service: Any, settings: Settings, thread_id: str, lead: Lead, stage: str) -> None:
    wanted = labels_for_lead(
        country_iso2=lead.country_iso2,
        category_group=load_categories().display_name(lead.category_group),
        website_status=lead.website_status,
        stage=stage,
    )
    user = settings.gmail_user_id
    add = ensure_labels(service, wanted, user)
    remove = ensure_labels(service, other_stage_labels(stage), user)
    execute(
        service.users()
        .threads()
        .modify(
            userId=user,
            id=thread_id,
            body={"addLabelIds": list(add.values()), "removeLabelIds": list(remove.values())},
        )
    )


def _follow_up(
    service: Any,
    settings: Settings,
    session: Session,
    row: Outreach,
    lead: Lead,
    state: ThreadState,
) -> bool:
    """One follow-up draft in the same thread. The row goes first: never two."""
    recipient = row.recipient or lead.email
    if not recipient or not row.gmail_thread_id:
        return False
    row_id = session.scalar(
        insert(Outreach)
        .values(
            lead_id=lead.lead_id,
            channel="email",
            stage=Stage.FOLLOWUP.value,
            is_followup=True,
            recipient=recipient,
            gmail_thread_id=row.gmail_thread_id,
            drafted_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing()
        .returning(Outreach.id)
    )
    if row_id is None:
        return False
    session.commit()
    language = lead_language(lead.name, lead.country_iso2)
    context = build_context(
        business_name=display_name(lead.name, language),
        category=lead.category_group,
        city=lead.city,
        website_status=lead.website_status,
        domain=lead.domain,
        sender=settings.sender,
        delivery_days=settings.outreach.delivery_days,
        price=settings.outreach.price,
    )
    text = render("email_followup", context, language).body.strip()
    subject = state.subject if state.subject.lower().startswith("re:") else f"Re: {state.subject}"
    message = build_mime(
        recipient,
        subject,
        f"{text}\n\n{ai.signature(settings.sender, language)}\n\n{LANGUAGES[language].opt_out}\n",
    )
    if state.sent_message_id:
        message["In-Reply-To"] = state.sent_message_id
        message["References"] = state.sent_message_id
    body = {"message": {**encode_message(message), "threadId": row.gmail_thread_id}}
    draft = execute(
        service.users().drafts().create(userId=settings.gmail_user_id, body=body),
        idempotent=False,
    )
    session.execute(
        update(Outreach).where(Outreach.id == row_id).values(gmail_draft_id=draft["id"])
    )
    session.commit()
    _relabel(service, settings, row.gmail_thread_id, lead, Stage.FOLLOWUP)
    return True


def _delete_pending_followup(
    service: Any, settings: Settings, session: Session, lead_id: str
) -> None:
    pending = session.scalars(
        select(Outreach).where(
            Outreach.lead_id == lead_id,
            Outreach.is_followup.is_(True),
            Outreach.sent_at.is_(None),
            Outreach.gmail_draft_id.is_not(None),
        )
    ).all()
    for row in pending:
        try:
            execute(
                service.users()
                .drafts()
                .delete(userId=settings.gmail_user_id, id=row.gmail_draft_id)
            )
        except Exception as exc:
            if not _missing(exc):
                raise
        row.gmail_draft_id = None


def _sync_row(
    service: Any, settings: Settings, session: Session, row: Outreach, now: datetime,
    counts: dict[str, int],
) -> None:  # fmt: skip
    user = settings.gmail_user_id
    lead = session.get(Lead, row.lead_id)
    if lead is None or not row.gmail_thread_id:
        return
    if row.sent_at is None:
        if not row.gmail_draft_id or _draft_exists(service, user, row.gmail_draft_id):
            return
        state = read_thread(_thread(service, user, row.gmail_thread_id))
        if state.sent_at is None:
            row.gmail_draft_id = None  # the draft was deleted instead of sent
            counts["deleted"] += 1
            return
        row.sent_at = state.sent_at
        if not row.is_followup:
            row.followup_due_at = followup_due_at(
                state.sent_at, settings.outreach.followup_after_days
            )
        counts["sent"] += 1
        _relabel(service, settings, row.gmail_thread_id, lead, row.stage)
    else:
        state = read_thread(_thread(service, user, row.gmail_thread_id))

    recipient = row.recipient or lead.email
    if state.bounced and not row.bounced:
        row.bounced = True
        counts["bounced"] += 1
        if recipient:
            suppress(session, recipient, "hard_bounce")
        _delete_pending_followup(service, settings, session, lead.lead_id)
    if state.replied_at and row.replied_at is None:
        row.replied_at, row.stage = state.replied_at, Stage.REPLIED
        counts["replied"] += 1
        _delete_pending_followup(service, settings, session, lead.lead_id)
        _relabel(service, settings, row.gmail_thread_id, lead, Stage.REPLIED)
    if state.opted_out and not row.opted_out:
        row.opted_out, row.stage = True, Stage.OPTED_OUT
        counts["opted_out"] += 1
        if recipient:
            suppress(session, recipient, "opt_out")
        _relabel(service, settings, row.gmail_thread_id, lead, Stage.OPTED_OUT)
    session.commit()

    if (
        row.is_followup
        or row.bounced
        or row.replied_at
        or row.opted_out
        or row.followup_due_at is None
        or row.followup_due_at > now
        or lead.status != LeadStatus.ACTIVE
        or lead.sanctions_hit
    ):
        return
    keys = [k.lower() for k in (recipient, lead.phone_e164) if k]
    if session.scalar(select(exists().where(Suppression.email_or_phone.in_(keys)))):
        return
    if _follow_up(service, settings, session, row, lead, state):
        counts["followups"] += 1


def sync(*, now: datetime | None = None, service: Any = None) -> dict[str, int]:
    """One pass over the Gmail threads of drafted leads. Creates drafts, never sends."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    counts = dict.fromkeys(
        ("sent", "deleted", "replied", "opted_out", "bounced", "followups", "failed"), 0
    )
    if service is None:
        from leads.google_auth import gmail_service

        service = gmail_service(settings)
    check_account(service, settings)
    recent = now - timedelta(days=WATCH_DAYS)
    with session_scope() as session:
        rows = session.scalars(
            select(Outreach.id).where(
                Outreach.channel == "email",
                Outreach.gmail_thread_id.is_not(None),
                Outreach.opted_out.is_(False),
                (Outreach.sent_at.is_(None) & Outreach.gmail_draft_id.is_not(None))
                | (Outreach.sent_at >= recent),
            )
            .order_by(Outreach.id)
        ).all()  # fmt: skip
    for row_id in rows:
        try:
            with session_scope() as session:
                row = session.get(Outreach, row_id)
                if row:
                    _sync_row(service, settings, session, row, now, counts)
        except Exception as exc:  # one bad thread must not stop the opt-outs of the others
            counts["failed"] += 1
            log.warning("gmail_sync_failed", outreach_id=row_id, error=repr(exc))
    return counts
