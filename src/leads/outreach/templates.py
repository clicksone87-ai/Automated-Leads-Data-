"""Render email / WhatsApp templates from config/templates, per language."""

from __future__ import annotations

import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leads.models import WebsiteStatus
from leads.settings import SenderSettings, config_dir

SUPPORTED_LANGUAGES = ("en", "fr", "de", "es", "it", "pt", "nl", "pl", "ar", "ru", "tr")

# Primary outreach language per country; everything else -> English.
# Multilingual countries (BE, CH, CA, ...) use the most common business language for
# now. TODO(build step 7): refine with region (e.g. Quebec -> fr, Flanders -> nl).
LANGUAGE_BY_COUNTRY: dict[str, str] = {
    **dict.fromkeys(["FR", "MC", "LU", "BE", "RE", "YT", "GP", "MQ", "GF", "PF", "NC", "PM",
                     "SN", "CI", "CM", "BJ", "BF", "ML", "NE", "TG", "GA", "CG", "CD", "MG",
                     "DJ", "KM", "TD", "CF", "GN", "BI", "HT"], "fr"),
    **dict.fromkeys(["DE", "AT", "CH", "LI"], "de"),
    **dict.fromkeys(["ES", "MX", "AR", "BO", "CL", "CO", "CR", "CU", "DO", "EC", "SV", "GT",
                     "HN", "NI", "PA", "PY", "PE", "PR", "UY", "VE", "GQ"], "es"),
    **dict.fromkeys(["IT", "SM", "VA"], "it"),
    **dict.fromkeys(["PT", "BR", "AO", "MZ", "CV", "GW", "ST"], "pt"),
    **dict.fromkeys(["NL", "SR", "AW", "CW", "SX"], "nl"),
    "PL": "pl",
    **dict.fromkeys(["SA", "AE", "QA", "KW", "BH", "OM", "JO", "LB", "IQ", "SY", "YE", "PS",
                     "EG", "LY", "SD", "MR", "DZ", "MA", "TN"], "ar"),
    **dict.fromkeys(["RU", "BY", "KZ"], "ru"),
    "TR": "tr",
}  # fmt: skip

# website_line per language. TODO(build step 7): add translations alongside i18n templates.
WEBSITE_LINES: dict[str, dict[str, str]] = {
    "en": {
        WebsiteStatus.NONE: "don't have a website yet",
        WebsiteStatus.BROKEN: "your website at {domain} isn't loading right now",
        WebsiteStatus.PARKED: "your website at {domain} isn't loading right now",
        # Not in the original brief; review this copy before drafting these segments.
        WebsiteStatus.REDIRECT_SOCIAL: "rely on a social media page rather than your own website",
        WebsiteStatus.WORKING: (
            "already have a website at {domain}, and it could be bringing in more enquiries"
        ),
    }
}


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    subject: str | None
    body: str


class MissingPlaceholderError(KeyError):
    pass


class _StrictDict(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise MissingPlaceholderError(key)


def template_dir() -> Path:
    return config_dir() / "templates"


def language_for_country(country_iso2: str | None, default: str = "en") -> str:
    return LANGUAGE_BY_COUNTRY.get((country_iso2 or "").upper(), default)


def template_path(name: str, language: str = "en", base: Path | None = None) -> Path:
    """Translated template if it exists, else the English one."""
    base = base or template_dir()
    translated = base / "i18n" / language / f"{name}.txt"
    return translated if language != "en" and translated.exists() else base / f"{name}.txt"


def effective_language(name: str, language: str, base: Path | None = None) -> str:
    base = base or template_dir()
    return language if template_path(name, language, base).parent.name == language else "en"


def placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def split_subject(text: str) -> tuple[str | None, str]:
    """A first line ``Subject: ...`` becomes the subject; the rest is the body."""
    first, _, rest = text.partition("\n")
    if first.lower().startswith("subject:"):
        return first.split(":", 1)[1].strip(), rest.lstrip("\n")
    return None, text


def website_line(status: str | None, domain: str | None, language: str = "en") -> str:
    lines = WEBSITE_LINES.get(language, WEBSITE_LINES["en"])
    key = WebsiteStatus(status) if status else WebsiteStatus.NONE
    return lines[key].format(domain=domain or "your domain")


def render(
    name: str, context: dict[str, Any], language: str = "en", base: Path | None = None
) -> RenderedMessage:
    """Render ``config/templates/[i18n/<lang>/]<name>.txt`` with ``context``.

    Raises :class:`MissingPlaceholderError` if the template uses a key not in ``context``.
    """
    text = template_path(name, language, base).read_text(encoding="utf-8")
    subject, body = split_subject(text)
    values = _StrictDict(context)
    return RenderedMessage(
        subject=subject.format_map(values) if subject else None,
        body=body.format_map(values).rstrip() + "\n",
    )


def build_context(
    *,
    business_name: str,
    category: str | None,
    city: str | None,
    website_status: str | None,
    domain: str | None,
    sender: SenderSettings,
    delivery_days: int,
    price: str,
    language: str = "en",
    link: str | None = None,
) -> dict[str, Any]:
    return {
        "business_name": business_name,
        "category": (category or "local").lower(),
        "city": city or "your area",
        "website_line": website_line(website_status, domain, language),
        "domain": domain or "",
        "delivery_days": delivery_days,
        "price": price,
        "sender_name": sender.sender_name,
        "company": sender.company,
        "company_website": sender.company_website,
        "company_phone": sender.company_phone,
        "company_address": sender.company_address,
        "link": link or sender.mockup_link,
    }
