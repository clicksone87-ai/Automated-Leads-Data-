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

WEBSITE_LINES: dict[str, dict[str, str]] = {
    "en": {
        WebsiteStatus.NONE: "don't have a website yet",
        WebsiteStatus.BROKEN: "your website at {domain} isn't loading right now",
        WebsiteStatus.PARKED: "your website at {domain} isn't loading right now",
        WebsiteStatus.REDIRECT_SOCIAL: "rely on a social media page rather than your own website",
        WebsiteStatus.WORKING: (
            "already have a website at {domain}, and it could be bringing in more enquiries"
        ),
    },
    "ar": {
        WebsiteStatus.NONE: "ليس لديكم موقع إلكتروني بعد",
        WebsiteStatus.BROKEN: "موقعكم الإلكتروني على {domain} لا يعمل حاليًا",
        WebsiteStatus.PARKED: "موقعكم الإلكتروني على {domain} لا يعمل حاليًا",
        WebsiteStatus.REDIRECT_SOCIAL: "تعتمدون على صفحة في وسائل التواصل الاجتماعي بدلًا من موقع خاص بكم",
        WebsiteStatus.WORKING: "لديكم موقع إلكتروني على {domain} ويمكن أن يجذب لكم المزيد من الاستفسارات",
    },
    "fr": {
        WebsiteStatus.NONE: "n'avez pas encore de site web",
        WebsiteStatus.BROKEN: "votre site web sur {domain} ne se charge pas en ce moment",
        WebsiteStatus.PARKED: "votre site web sur {domain} ne se charge pas en ce moment",
        WebsiteStatus.REDIRECT_SOCIAL: "utilisez une page sur les réseaux sociaux plutôt qu'un site qui vous appartient",
        WebsiteStatus.WORKING: "avez déjà un site web sur {domain}, qui pourrait vous apporter plus de demandes",
    },
    "de": {
        WebsiteStatus.NONE: "noch keine eigene Website haben",
        WebsiteStatus.BROKEN: "Ihre Website unter {domain} gerade nicht erreichbar ist",
        WebsiteStatus.PARKED: "Ihre Website unter {domain} gerade nicht erreichbar ist",
        WebsiteStatus.REDIRECT_SOCIAL: "statt einer eigenen Website nur eine Social-Media-Seite nutzen",
        WebsiteStatus.WORKING: "bereits eine Website unter {domain} haben, die noch mehr Anfragen bringen könnte",
    },
    "es": {
        WebsiteStatus.NONE: "aún no tienen un sitio web",
        WebsiteStatus.BROKEN: "su sitio web en {domain} no está cargando ahora mismo",
        WebsiteStatus.PARKED: "su sitio web en {domain} no está cargando ahora mismo",
        WebsiteStatus.REDIRECT_SOCIAL: "utilizan una página en redes sociales en lugar de un sitio web propio",
        WebsiteStatus.WORKING: "ya tienen un sitio web en {domain} que podría generar más consultas",
    },
    "it": {
        WebsiteStatus.NONE: "non avete ancora un sito web",
        WebsiteStatus.BROKEN: "il vostro sito web su {domain} non si carica al momento",
        WebsiteStatus.PARKED: "il vostro sito web su {domain} non si carica al momento",
        WebsiteStatus.REDIRECT_SOCIAL: "utilizzate una pagina sui social media invece di un vostro sito web",
        WebsiteStatus.WORKING: "avete già un sito web su {domain} che potrebbe portarvi più richieste",
    },
    "pt": {
        WebsiteStatus.NONE: "ainda não têm um site",
        WebsiteStatus.BROKEN: "o vosso site em {domain} não está a carregar de momento",
        WebsiteStatus.PARKED: "o vosso site em {domain} não está a carregar de momento",
        WebsiteStatus.REDIRECT_SOCIAL: "utilizam uma página nas redes sociais em vez de um site próprio",
        WebsiteStatus.WORKING: "já têm um site em {domain} que poderia gerar mais contactos",
    },
    "nl": {
        WebsiteStatus.NONE: "nog geen eigen website hebben",
        WebsiteStatus.BROKEN: "uw website op {domain} op dit moment niet laadt",
        WebsiteStatus.PARKED: "uw website op {domain} op dit moment niet laadt",
        WebsiteStatus.REDIRECT_SOCIAL: "een social-mediapagina gebruikt in plaats van een eigen website",
        WebsiteStatus.WORKING: "al een website heeft op {domain} die nog meer aanvragen zou kunnen opleveren",
    },
    "pl": {
        WebsiteStatus.NONE: "nie mają jeszcze własnej strony internetowej",
        WebsiteStatus.BROKEN: "Państwa strona internetowa pod adresem {domain} aktualnie nie działa",
        WebsiteStatus.PARKED: "Państwa strona internetowa pod adresem {domain} aktualnie nie działa",
        WebsiteStatus.REDIRECT_SOCIAL: "korzystają z profilu w mediach społecznościowych zamiast własnej strony",
        WebsiteStatus.WORKING: "mają już stronę internetową pod adresem {domain}, która mogłaby przynosić więcej zapytań",
    },
    "ru": {
        WebsiteStatus.NONE: "у вас ещё нет собственного сайта",
        WebsiteStatus.BROKEN: "ваш сайт на {domain} сейчас не загружается",
        WebsiteStatus.PARKED: "ваш сайт на {domain} сейчас не загружается",
        WebsiteStatus.REDIRECT_SOCIAL: "используете страницу в социальных сетях вместо собственного сайта",
        WebsiteStatus.WORKING: "у вас уже есть сайт на {domain}, который мог бы приносить больше обращений",
    },
    "tr": {
        WebsiteStatus.NONE: "henüz bir web siteniz bulunmuyor",
        WebsiteStatus.BROKEN: "{domain} adresindeki web siteniz şu an açılmıyor",
        WebsiteStatus.PARKED: "{domain} adresindeki web siteniz şu an açılmıyor",
        WebsiteStatus.REDIRECT_SOCIAL: "kendi web siteniz yerine bir sosyal medya sayfası kullanıyorsunuz",
        WebsiteStatus.WORKING: "{domain} adresinde zaten bir web siteniz var ve daha fazla talep getirebilir",
    },
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
