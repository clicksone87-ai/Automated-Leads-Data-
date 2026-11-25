"""Fixed email sentences per language. A language is usable only when it is complete here."""

from __future__ import annotations

import re
from dataclasses import dataclass

from leads.models import WebsiteStatus
from leads.outreach.templates import language_for_country


@dataclass(frozen=True, slots=True)
class Phrases:
    name: str  # for the AI instructions and the dashboard
    greeting: str
    regards: str
    team: str
    opt_out: str
    openings: dict[str, tuple[str, ...]]
    words: tuple[int, int]  # accepted length of the AI text


EN = Phrases(
    name="British English",
    greeting="Hi {name} team,",
    regards="Best regards,",
    team="The {company} team",
    opt_out="If you'd rather not hear from us, reply \"no thanks\" and we won't contact you again.",
    # "None" is absence of evidence and is worded as such.
    openings={
        WebsiteStatus.NONE: (
            "We came across {name} and couldn't find a website for the business.",
            "We looked for a website for {name} and couldn't find one.",
            "We came across {name}, but not a website for it.",
        ),
        WebsiteStatus.BROKEN: (
            "We tried to visit {domain} and the site didn't load properly for us.",
            "We came across {name}, but {domain} didn't open properly when we tried it.",
        ),
        WebsiteStatus.PARKED: (
            "We came across {name}, but {domain} showed only a placeholder page when we opened it.",
        ),
        WebsiteStatus.REDIRECT_SOCIAL: (
            "We came across {name} and found a social media page, but no separate website.",
        ),
        WebsiteStatus.WORKING: ("We came across {name} and your website at {domain}.",),
    },
    words=(30, 140),
)

AR = Phrases(
    name="Modern Standard Arabic",
    greeting="فريق {name} المحترم،\nالسلام عليكم ورحمة الله وبركاته،",
    regards="مع خالص التحية،",
    team="فريق {company}",
    opt_out=(
        "إذا كنتم لا ترغبون في تلقي رسائل منا، يكفي الرد بعبارة «لا شكرا» ولن نتواصل معكم مرة أخرى."
    ),
    openings={
        WebsiteStatus.NONE: (
            "اطّلعنا على نشاط {name} ولم نعثر على موقع إلكتروني له.",
            "بحثنا عن موقع إلكتروني لـ {name} ولم نعثر عليه.",
        ),
        WebsiteStatus.BROKEN: (
            "حاولنا زيارة {domain} ولكن الموقع لم يُفتح لدينا بشكل صحيح.",
            "اطّلعنا على نشاط {name}، ولكن الموقع {domain} لم يُفتح بشكل صحيح عندما حاولنا زيارته.",
        ),
        WebsiteStatus.PARKED: (
            "اطّلعنا على نشاط {name}، ولكن {domain} عرض لنا صفحة مؤقتة فقط عند فتحه.",
        ),
        WebsiteStatus.REDIRECT_SOCIAL: (
            "اطّلعنا على نشاط {name} ووجدنا صفحة على وسائل التواصل الاجتماعي، "
            "ولم نعثر على موقع إلكتروني مستقل.",
        ),
        WebsiteStatus.WORKING: ("اطّلعنا على نشاط {name} وعلى موقعكم الإلكتروني {domain}.",),
    },
    words=(25, 130),
)

LANGUAGES: dict[str, Phrases] = {"en": EN, "ar": AR}

_ARABIC = re.compile(r"[ء-يٮ-ۓ]")
_LATIN = re.compile(r"[A-Za-zÀ-ÿ]")


def lead_language(name: str, country_iso2: str | None) -> str:
    """The language a lead is written to.

    In Arabic-speaking countries many businesses trade in English, so the script of their
    own name decides; elsewhere the country does.
    """
    language = language_for_country(country_iso2)
    if language == "ar" and len(_ARABIC.findall(name)) <= len(_LATIN.findall(name)):
        return "en"
    return language


def display_name(name: str, language: str) -> str:
    """The part of a bilingual name ("Al Noor | النور") in the email's language."""
    wanted, other = (_ARABIC, _LATIN) if language == "ar" else (_LATIN, _ARABIC)
    parts = [p.strip() for p in re.split(r"\s*[|/]\s*|\s+[-–]\s+", name) if p.strip()]
    if len(parts) < 2:
        return name
    own = [p for p in parts if wanted.search(p) and not other.search(p)]
    foreign = [p for p in parts if other.search(p) and not wanted.search(p)]
    return own[0] if own and foreign else name


def readable(text: str | None, language: str) -> str | None:
    """``text`` unless it is in a script the reader of this language would not expect."""
    if not text:
        return None
    for char in text:
        if not char.isalpha() or _LATIN.match(char):
            continue
        if language == "ar" and _ARABIC.match(char):
            continue
        if language != "ar" and ord(char) < 0x250:  # accented Latin letters
            continue
        return None
    return text
