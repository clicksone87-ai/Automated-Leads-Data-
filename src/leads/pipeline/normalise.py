"""Normalisation of raw source fields: phone, domain, name, address, country, category."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import phonenumbers
import polars as pl
import tldextract
from phonenumbers import PhoneNumberFormat, PhoneNumberType

from leads.settings import CategoryConfig

# --- phone -----------------------------------------------------------------------------

_PHONE_TYPES: dict[int, str] = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "landline_or_mobile",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.PREMIUM_RATE: "premium_rate",
    PhoneNumberType.SHARED_COST: "shared_cost",
    PhoneNumberType.PERSONAL_NUMBER: "personal",
    PhoneNumberType.UAN: "uan",
}


@dataclass(frozen=True, slots=True)
class Phone:
    e164: str
    type: str  # mobile | landline | landline_or_mobile | voip | toll_free | ... | other


def normalise_phone(raw: str | None, country_iso2: str | None = None) -> Phone | None:
    """Parse a phone number into E.164 + type. Returns None if missing or invalid.

    ``country_iso2`` is the default region for numbers written without a ``+`` prefix.
    """
    if not raw or not raw.strip():
        return None
    cleaned = raw.replace("(0)", " ").strip()
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    region = country_iso2.upper() if country_iso2 else None
    try:
        number = phonenumbers.parse(cleaned, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    e164 = phonenumbers.format_number(number, PhoneNumberFormat.E164)
    return Phone(e164=e164, type=_PHONE_TYPES.get(phonenumbers.number_type(number), "other"))


# --- domain / website ------------------------------------------------------------------

SOCIAL_DOMAINS: dict[str, str] = {
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "fb.me": "facebook",
    "instagram.com": "instagram",
    "linkedin.com": "linkedin",
    "twitter.com": "x",
    "x.com": "x",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "pinterest.com": "pinterest",
}
# A site that only redirects here counts as "redirect_social" (see enrich.website).
REDIRECT_SOCIAL_DOMAINS = frozenset({"facebook.com", "fb.com", "fb.me", "instagram.com"})

# Website builders not in the Public Suffix List: keep the full host so that unrelated
# businesses on the same builder are not merged by domain.
SHARED_HOSTING_DOMAINS = frozenset(
    {
        "wixsite.com",
        "business.site",
        "square.site",
        "godaddysites.com",
        "webnode.page",
        "jimdosite.com",
        "weebly.com",
        "wordpress.com",
        "site123.me",
        "mystrikingly.com",
        "carrd.co",
        "ueniweb.com",
        "google.com",
    }
)


@lru_cache(maxsize=1)
def _tld_extractor() -> tldextract.TLDExtract:
    # Offline: use the Public Suffix List snapshot bundled with tldextract (no HTTP fetch).
    return tldextract.TLDExtract(
        suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
    )


def host_of(url: str | None) -> str | None:
    """Lower-cased host of a URL (or bare domain), without ``www.`` and port."""
    if not url or not url.strip():
        return None
    value = url.strip()
    if "://" not in value:
        value = "http://" + value.lstrip("/")
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    return host[4:] if host.startswith("www.") else host


def registered_domain(host: str | None) -> str | None:
    """``shop.example.co.uk`` -> ``example.co.uk``. None for IPs / hosts without a suffix."""
    if not host:
        return None
    ext = _tld_extractor()(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def normalise_domain(url_or_domain: str | None) -> str | None:
    """Domain used for matching and labels.

    Returns the registered domain (``https://www.Shop.Example.co.uk/x`` -> ``example.co.uk``),
    the full host for shared website builders (``joe.wixsite.com``), or None if invalid.
    Social profile URLs return their social domain (check with :func:`is_social_domain`).
    """
    host = host_of(url_or_domain)
    reg = registered_domain(host)
    if host is None or reg is None:
        return None
    if reg in SHARED_HOSTING_DOMAINS and host != reg:
        return host
    return reg


def is_social_domain(domain: str | None) -> bool:
    return domain is not None and domain in SOCIAL_DOMAINS


def normalise_website(url: str | None) -> str | None:
    """Clean a website URL; adds ``https://`` when no scheme is given."""
    if not url or not url.strip():
        return None
    value = url.strip()
    if value.lower().startswith(("mailto:", "tel:")):
        return None
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    return value if host_of(value) else None


# --- names -----------------------------------------------------------------------------

# Legal-form tokens removed from the END of a name (after dots are removed and casefolding).
LEGAL_SUFFIXES = frozenset(
    {
        "ltd", "limited", "llc", "llp", "lp", "inc", "incorporated", "corp", "corporation",
        "co", "company", "plc", "pty", "pvt", "gmbh", "mbh", "ag", "kg", "ohg", "ug", "ev",
        "sa", "sas", "sasu", "sarl", "eurl", "sl", "slu", "srl", "snc", "bv", "nv", "vof",
        "as", "asa", "ab", "oy", "oyj", "aps", "ehf", "kft", "bt", "zrt", "doo", "dd", "sro",
        "ou", "sia", "uab", "jsc", "pjsc", "ojsc", "cjsc", "ltda", "sac", "eirl", "cia",
        "sti", "fze", "fzco", "fzllc", "wll", "spc", "ооо", "ао", "пао", "зао", "оао", "ип",
        "ذمم",
    }
)  # fmt: skip
# Multi-token legal forms (after punctuation removal), checked at the end of the name.
LEGAL_SUFFIX_SEQUENCES: tuple[tuple[str, ...], ...] = (
    ("sp", "z", "oo"),
    ("ltd", "sti"),
    ("fz", "llc"),
    ("pty", "ltd"),
    ("pvt", "ltd"),
    ("co", "ltd"),
)
# Legal-form tokens removed from the START (e.g. Russian "ООО Ромашка").
LEGAL_PREFIXES = frozenset({"the", "ооо", "ао", "пао", "зао", "оао", "ип"})

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise_name(name: str | None) -> str:
    """Matching key for a business name: casefold, no accents/punctuation/legal forms.

    ``"Café de l'Étoile S.A.R.L."`` -> ``"cafe de l etoile"``.
    """
    if not name:
        return ""
    value = strip_accents(name).casefold().replace("&", " and ").replace(".", "")
    tokens = _NON_WORD.sub(" ", value).replace("_", " ").split()
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for seq in LEGAL_SUFFIX_SEQUENCES:
            if len(tokens) > len(seq) and tuple(tokens[-len(seq) :]) == seq:
                tokens = tokens[: -len(seq)]
                changed = True
        if len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
            tokens.pop()
            changed = True
    while len(tokens) > 1 and tokens[0] in LEGAL_PREFIXES:
        tokens.pop(0)
    return " ".join(tokens)


# --- address / country -----------------------------------------------------------------

COUNTRY_ALIASES = {"UK": "GB", "EL": "GR", "KS": "XK"}

_WS = re.compile(r"\s+")


def normalise_whitespace(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WS.sub(" ", value).strip(" ,")
    return cleaned or None


def normalise_country(value: str | None) -> str | None:
    """Upper-case ISO-2 code; maps common aliases (UK -> GB). None if not a 2-letter code."""
    if not value:
        return None
    code = value.strip().upper()
    code = COUNTRY_ALIASES.get(code, code)
    return code if len(code) == 2 and code.isalpha() else None


def normalise_postcode(value: str | None) -> str | None:
    """Compact postcode key: upper-case, no spaces or hyphens (``sw1a 1aa`` -> ``SW1A1AA``)."""
    if not value:
        return None
    compact = re.sub(r"[\s\-]", "", value).upper()
    return compact or None


def in_scope(country_iso2: str | None, countries: Collection[str]) -> bool:
    return country_iso2 is not None and country_iso2 in countries


# --- category --------------------------------------------------------------------------


def map_category(source: str, category: str | None, config: CategoryConfig) -> str:
    """Map a source category to a shared group key (see config/categories.yaml)."""
    if not category:
        return config.default_group
    lowered = category.strip().lower()
    exact = config.exact.get(source, {}).get(lowered)
    if exact:
        return exact
    for group, words in config.keywords.items():
        if any(word in lowered for word in words):
            return group
    return config.default_group


# --- frame-level -----------------------------------------------------------------------

NORMALISED_SCHEMA: dict[str, Any] = {
    "source": pl.String,
    "source_record_id": pl.String,
    "name": pl.String,
    "name_norm": pl.String,
    "category": pl.String,
    "category_group": pl.String,
    "phone_e164": pl.String,
    "phone_type": pl.String,
    "email": pl.String,
    "website": pl.String,
    "domain": pl.String,
    "address": pl.String,
    "city": pl.String,
    "region": pl.String,
    "postcode": pl.String,
    "postcode_key": pl.String,
    "country_iso2": pl.String,
    "lat": pl.Float64,
    "lon": pl.Float64,
    "created_date": pl.Date,
    "shared_ids": pl.List(pl.String),
}


def normalise_record(row: dict[str, Any], categories: CategoryConfig) -> dict[str, Any]:
    """Normalise one raw-zone row (see ``sources.base.RAW_SCHEMA``)."""
    country = normalise_country(row.get("country_iso2"))
    phone = normalise_phone(row.get("phone"), country)
    website = normalise_website(row.get("website"))
    email = (row.get("email") or "").strip().lower() or None
    return {
        "source": row["source"],
        "source_record_id": str(row["source_record_id"]),
        "name": normalise_whitespace(row.get("name")),
        "name_norm": normalise_name(row.get("name")),
        "category": row.get("category"),
        "category_group": map_category(row["source"], row.get("category"), categories),
        "phone_e164": phone.e164 if phone else None,
        "phone_type": phone.type if phone else None,
        "email": email,
        "website": website,
        "domain": normalise_domain(website),
        "address": normalise_whitespace(row.get("address")),
        "city": normalise_whitespace(row.get("city")),
        "region": normalise_whitespace(row.get("region")),
        "postcode": normalise_whitespace(row.get("postcode")),
        "postcode_key": normalise_postcode(row.get("postcode")),
        "country_iso2": country,
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "created_date": row.get("created_date"),
        "shared_ids": row.get("shared_ids") or [],
    }


def normalise_frame(
    raw: pl.DataFrame, categories: CategoryConfig, countries: Collection[str]
) -> pl.DataFrame:
    """Normalise a raw-zone frame and keep only rows in ``countries`` with a name.

    TODO(scale): row-wise Python is fine for test countries; vectorise hot paths
    (domain, postcode) with Polars expressions before running all countries.
    """
    rows = [normalise_record(r, categories) for r in raw.iter_rows(named=True)]
    frame = pl.DataFrame(rows, schema=NORMALISED_SCHEMA)
    return frame.filter(pl.col("country_iso2").is_in(list(countries)) & (pl.col("name_norm") != ""))
