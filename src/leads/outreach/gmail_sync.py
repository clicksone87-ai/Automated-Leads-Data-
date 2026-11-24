"""Gmail sync (every 30 min): detect sent / replied / bounced / opted out, re-apply labels
to the sent thread, and create ONE follow-up draft 6 days after a sent email with no reply.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

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


def sync(*, now: datetime | None = None) -> dict[str, int]:
    """One sync pass over Gmail.

    TODO(build step 7):
      1. Drafts: for outreach rows with gmail_draft_id and no sent_at, call drafts.get; if the
         draft is gone, find the sent message in its thread (threads.get) -> sent_at,
         gmail_thread_id, followup_due_at = followup_due_at(sent_at,
         settings.outreach.followup_after_days).
         (Use users.history.list with a stored historyId for efficiency later.)
      2. Re-apply labels to the sent thread (threads.modify): segment labels + stage label,
         removing ``labels.other_stage_labels``.
      3. Replies: thread messages not from the user after sent_at -> replied_at, stage
         'replied'. If ``is_opt_out(body)`` -> opted_out, stage 'opted_out', add the
         lead email to ``suppression`` (reason 'opt_out'), delete any pending follow-up draft.
      4. Bounces: ``is_hard_bounce`` messages referencing the thread -> bounced, suppression
         (reason 'hard_bounce').
      5. Follow-ups: rows with stage 'first', sent, no reply, not suppressed and
         followup_due_at <= now, and no follow-up yet -> render ``email_followup`` as a reply
         draft in the same thread (threadId + In-Reply-To/References headers), insert
         Outreach(stage='followup'). Exactly one follow-up per lead, ever.
      6. Return counts: {"sent": n, "replied": n, "opted_out": n, "bounced": n, "followups": n}.
    """
    raise NotImplementedError("gmail_sync.sync (build step 7)")
