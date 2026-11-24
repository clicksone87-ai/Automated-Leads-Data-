"""Website checker.

Classifies each lead's website as:

* ``working``         HTTP 200 with real content
* ``broken``          DNS failure, 4xx/5xx, SSL error, timeout, connection error, empty page
* ``parked``          parking/marketplace host or nameserver, or phrases like "domain for sale"
* ``redirect_social`` the site is (or redirects to) only a Facebook/Instagram page
* ``none``            no website at all

and extracts public emails, publicly shown WhatsApp numbers (with the URL of the page
showing them), social profile links and the earliest copyright year.

Politeness rules: only the home page and one contact page are fetched (at most
``max_requests_per_domain`` page requests), robots.txt is respected, 12 s timeout,
the User-Agent names the tool. WhatsApp numbers are only read from the page; no
WhatsApp API is ever called.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx
from selectolax.lexbor import LexborHTMLParser

from leads.enrich import parked
from leads.models import WebsiteStatus
from leads.pipeline.normalise import (
    REDIRECT_SOCIAL_DOMAINS,
    SOCIAL_DOMAINS,
    host_of,
    normalise_phone,
    normalise_website,
    registered_domain,
)
from leads.settings import WebsiteSettings


class FetchError(StrEnum):
    DNS = "dns"
    TIMEOUT = "timeout"
    SSL = "ssl"
    CONNECT = "connect"
    ROBOTS = "robots_disallowed"
    TOO_MANY_REDIRECTS = "too_many_redirects"


@dataclass(slots=True)
class PageFetch:
    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    html: str | None = None
    error: FetchError | None = None


@dataclass(slots=True)
class WhatsAppHit:
    number: str  # E.164
    evidence_url: str  # page where the number is publicly shown
    kind: str  # "link" | "text"


@dataclass(slots=True)
class Contacts:
    emails: list[str] = field(default_factory=list)
    whatsapp: list[WhatsAppHit] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    copyright_year: int | None = None


@dataclass(slots=True)
class WebsiteCheck:
    status: WebsiteStatus
    reason: str
    final_url: str | None = None
    contacts: Contacts = field(default_factory=Contacts)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# --- HTML helpers -----------------------------------------------------------------------


def parse_html(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def visible_text(html: str | None) -> str:
    """Body text without scripts/styles, whitespace collapsed."""
    if not html:
        return ""
    tree = parse_html(html)
    tree.strip_tags(["script", "style", "noscript", "template", "svg"])
    root = tree.body or tree.root
    text = root.text(separator=" ", strip=True) if root is not None else ""
    return re.sub(r"\s+", " ", text).strip()


def _hrefs(html: str) -> list[tuple[str, str]]:
    """(href, link text) for every anchor."""
    tree = parse_html(html)
    out: list[tuple[str, str]] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if href:
            out.append((href, node.text(strip=True)))
    return out


# --- classification ---------------------------------------------------------------------


def classify(
    website: str | None,
    home: PageFetch | None,
    *,
    nameservers: tuple[str, ...] = (),
    min_text_chars: int = 100,
) -> tuple[WebsiteStatus, str]:
    """Classify a website from its home-page fetch. Pure function (no network)."""
    if not website or not website.strip():
        return WebsiteStatus.NONE, "no website"
    site_domain = registered_domain(host_of(website))
    if site_domain in REDIRECT_SOCIAL_DOMAINS:
        return WebsiteStatus.REDIRECT_SOCIAL, "website is a social profile"
    if parked.has_parked_nameservers(nameservers):
        return WebsiteStatus.PARKED, "parking nameservers"
    if home is None:
        return WebsiteStatus.BROKEN, "not fetched"
    if home.error == FetchError.ROBOTS:
        return WebsiteStatus.WORKING, "robots.txt disallows fetching; server responds"
    if home.error is not None:
        return WebsiteStatus.BROKEN, str(home.error)

    final_host = host_of(home.final_url or home.requested_url)
    if registered_domain(final_host) in REDIRECT_SOCIAL_DOMAINS:
        return WebsiteStatus.REDIRECT_SOCIAL, f"redirects to {final_host}"
    if parked.is_parked_host(final_host):
        return WebsiteStatus.PARKED, f"parking host {final_host}"
    if home.status_code is None or home.status_code >= 400:
        return WebsiteStatus.BROKEN, f"http {home.status_code}"

    text = visible_text(home.html)
    title_node = parse_html(home.html).css_first("title") if home.html else None
    title = title_node.text(strip=True) if title_node is not None else ""
    if parked.has_parked_phrases(f"{title} {text}"):
        return WebsiteStatus.PARKED, "parked phrases"
    if len(text) >= min_text_chars:
        return WebsiteStatus.WORKING, "200 with content"
    # JS-rendered sites (Wix/Squarespace/React) have little server-side text but load scripts.
    if title and home.html and "<script" in home.html.lower():
        return WebsiteStatus.WORKING, "200, JS-rendered"
    return WebsiteStatus.BROKEN, "200 but empty page"


# --- extraction -------------------------------------------------------------------------

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}"
)
_JUNK_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "domain.com", "email.com", "sentry.io", "wixpress.com",
     "sentry-next.wixpress.com", "yourdomain.com", "test.com"}
)  # fmt: skip
_JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


def _clean_email(value: str) -> str | None:
    email = unquote(value).strip().strip(".,;:").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.partition("@")
    if domain in _JUNK_EMAIL_DOMAINS or email.endswith(_JUNK_EMAIL_SUFFIXES) or len(local) > 64:
        return None
    return email


def extract_emails(html: str) -> list[str]:
    """Public emails from mailto: links and page text, in page order, de-duplicated."""
    found: list[str] = []
    for href, _ in _hrefs(html):
        if href.lower().startswith("mailto:"):
            for part in href[7:].split("?", 1)[0].split(","):
                if (email := _clean_email(part)) and email not in found:
                    found.append(email)
    for match in EMAIL_RE.findall(visible_text(html)):
        if (email := _clean_email(match)) and email not in found:
            found.append(email)
    return found


_WA_TEXT_RE = re.compile(
    r"whats\s?app(?:\s*(?:us|number|no\.?|nr\.?|only))?\s*[:\-–]?\s*(\+?\d[\d\s().\-]{6,20}\d)",
    re.IGNORECASE,
)


def _wa_number_from_href(href: str) -> str | None:
    """Digits of the phone in wa.me / api.whatsapp.com / whatsapp:// links."""
    parts = urlsplit(href)
    host = (parts.hostname or "").lower()
    if host in {"wa.me", "www.wa.me"}:
        digits = re.sub(r"\D", "", parts.path)
        return digits or None
    if host.endswith("whatsapp.com") or parts.scheme.lower() == "whatsapp":
        phone = parse_qs(parts.query).get("phone", [""])[0]
        digits = re.sub(r"\D", "", phone)
        return digits or None
    return None


def extract_whatsapp(html: str, page_url: str, country_iso2: str | None) -> list[WhatsAppHit]:
    """WhatsApp numbers the business shows publicly: wa.me / api.whatsapp.com links,
    WhatsApp buttons (which are such links) and "WhatsApp: +..." text."""
    hits: list[WhatsAppHit] = []
    seen: set[str] = set()

    def add(raw: str, kind: str, international: bool) -> None:
        phone = normalise_phone(("+" + raw) if international else raw, country_iso2)
        if phone and phone.e164 not in seen:
            seen.add(phone.e164)
            hits.append(WhatsAppHit(number=phone.e164, evidence_url=page_url, kind=kind))

    for href, _ in _hrefs(html):
        if digits := _wa_number_from_href(href):
            add(digits, "link", international=True)  # wa.me numbers are always international
    for match in _WA_TEXT_RE.finditer(visible_text(html)):
        raw = match.group(1)
        add(raw, "text", international=False)
    return hits


_SHARE_PATH_HINTS = ("/sharer", "/share", "/intent/", "/dialog/", "/plugins/")


def extract_socials(html: str) -> dict[str, str]:
    """First profile link per network (share/intent links are ignored)."""
    socials: dict[str, str] = {}
    for href, _ in _hrefs(html):
        reg = registered_domain(host_of(href)) if href.lower().startswith("http") else None
        network = SOCIAL_DOMAINS.get(reg or "")
        if network and network not in socials:
            if any(hint in href.lower() for hint in _SHARE_PATH_HINTS):
                continue
            socials[network] = href
    return socials


_COPYRIGHT_RE = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:[A-Za-z.]+\s+)?((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?",
    re.IGNORECASE,
)


def extract_copyright_year(html: str, *, today: date | None = None) -> int | None:
    """Earliest plausible year in copyright notices (``© 2019-2024`` -> 2019)."""
    current = (today or date.today()).year
    years = [
        int(y)
        for m in _COPYRIGHT_RE.finditer(visible_text(html))
        for y in m.groups()
        if y and 1990 <= int(y) <= current
    ]
    return min(years) if years else None


def extract_contacts(html: str, page_url: str, country_iso2: str | None) -> Contacts:
    return Contacts(
        emails=extract_emails(html),
        whatsapp=extract_whatsapp(html, page_url, country_iso2),
        socials=extract_socials(html),
        copyright_year=extract_copyright_year(html),
    )


def merge_contacts(*items: Contacts) -> Contacts:
    merged = Contacts()
    for c in items:
        merged.emails += [e for e in c.emails if e not in merged.emails]
        known = {h.number for h in merged.whatsapp}
        merged.whatsapp += [h for h in c.whatsapp if h.number not in known]
        for k, v in c.socials.items():
            merged.socials.setdefault(k, v)
        years = [y for y in (merged.copyright_year, c.copyright_year) if y]
        merged.copyright_year = min(years) if years else None
    return merged


_CONTACT_HINTS = (
    "contact", "kontakt", "contacto", "contato", "contatti", "contacter", "contactez",
    "iletisim", "iletişim", "kontakty", "контакт", "اتصل", "تواصل", "impressum",
)  # fmt: skip


def find_contact_url(html: str, base_url: str) -> str | None:
    """Same-site link whose href or text looks like a contact page."""
    base_host = registered_domain(host_of(base_url))
    for href, label in _hrefs(html):
        target = urljoin(base_url, href)
        if not target.lower().startswith("http"):
            continue
        if registered_domain(host_of(target)) != base_host:
            continue
        haystack = f"{href} {label}".lower()
        if any(hint in haystack for hint in _CONTACT_HINTS):
            return target
    return None


# --- network ------------------------------------------------------------------------------


async def resolve_dns(domain: str, timeout: float = 5.0) -> tuple[bool, tuple[str, ...]]:
    """(resolves?, nameservers) for a domain."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout
    ok = False
    for rtype in ("A", "AAAA"):
        try:
            await resolver.resolve(domain, rtype)
            ok = True
            break
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            break
        except (dns.resolver.NoAnswer, dns.exception.Timeout):
            continue
    nameservers: tuple[str, ...] = ()
    reg = registered_domain(domain)
    if ok and reg:
        try:
            answer = await resolver.resolve(reg, "NS")
            nameservers = tuple(str(r.target).rstrip(".").lower() for r in answer)
        except dns.exception.DNSException:
            pass
    return ok, nameservers


async def fetch_page(client: httpx.AsyncClient, url: str) -> PageFetch:
    result = PageFetch(requested_url=url)
    try:
        response = await client.get(url)
    except httpx.TimeoutException:
        result.error = FetchError.TIMEOUT
    except httpx.TooManyRedirects:
        result.error = FetchError.TOO_MANY_REDIRECTS
    except httpx.ConnectError as exc:
        is_ssl = isinstance(exc.__cause__, ssl.SSLError) or "ssl" in str(exc).lower()
        result.error = FetchError.SSL if is_ssl else FetchError.CONNECT
    except httpx.HTTPError:
        result.error = FetchError.CONNECT
    else:
        result.final_url = str(response.url)
        result.status_code = response.status_code
        content_type = response.headers.get("content-type", "")
        if "html" in content_type or not content_type:
            result.html = response.text[:2_000_000]
    return result


async def _robots(client: httpx.AsyncClient, base_url: str) -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser()
    try:
        response = await client.get(urljoin(base_url, "/robots.txt"))
        lines = response.text.splitlines() if response.status_code == 200 else []
    except httpx.HTTPError:
        lines = []
    parser.parse(lines)  # empty -> everything allowed
    return parser


def make_client(cfg: WebsiteSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=cfg.timeout_seconds,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": cfg.user_agent, "Accept": "text/html,*/*;q=0.5"},
    )


async def check_website(
    website: str | None,
    *,
    country_iso2: str | None,
    client: httpx.AsyncClient,
    cfg: WebsiteSettings,
) -> WebsiteCheck:
    """Check one website: DNS, robots.txt, home page, then (if working) one contact page."""
    url = normalise_website(website)
    if url is None:
        return WebsiteCheck(WebsiteStatus.NONE, "no website")
    status, reason = classify(url, PageFetch(url))  # catches social-profile websites
    if status == WebsiteStatus.REDIRECT_SOCIAL:
        return WebsiteCheck(status, reason, final_url=url)

    host = host_of(url) or ""
    resolves, nameservers = await resolve_dns(host)
    if not resolves:
        return WebsiteCheck(WebsiteStatus.BROKEN, "dns")

    budget = cfg.max_requests_per_domain
    robots = await _robots(client, url) if cfg.respect_robots_txt else None

    def allowed(target: str) -> bool:
        return robots is None or robots.can_fetch(cfg.user_agent, target)

    if not allowed(url):
        home = PageFetch(url, error=FetchError.ROBOTS)
    else:
        home = await fetch_page(client, url)
        budget -= 1
        if (
            home.error in (FetchError.SSL, FetchError.CONNECT)
            and url.startswith("https://")
            and budget > 0
        ):
            home = await fetch_page(client, "http://" + url[len("https://") :])
            budget -= 1

    status, reason = classify(url, home, nameservers=nameservers, min_text_chars=cfg.min_text_chars)
    check = WebsiteCheck(status, reason, final_url=home.final_url)
    if status != WebsiteStatus.WORKING or not home.html:
        return check

    page_url = home.final_url or url
    contacts = [extract_contacts(home.html, page_url, country_iso2)]
    contact_url = find_contact_url(home.html, page_url)
    if contact_url and budget > 0 and allowed(contact_url):
        contact_page = await fetch_page(client, contact_url)
        if contact_page.html and (contact_page.status_code or 500) < 400:
            contacts.append(
                extract_contacts(
                    contact_page.html, contact_page.final_url or contact_url, country_iso2
                )
            )
    check.contacts = merge_contacts(*contacts)
    return check


async def check_many(
    items: list[tuple[str, str | None, str | None]], cfg: WebsiteSettings, concurrency: int = 50
) -> dict[str, WebsiteCheck]:
    """Check many websites concurrently. ``items`` = (key, website, country_iso2)."""
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, WebsiteCheck] = {}
    async with make_client(cfg) as client:

        async def one(key: str, website: str | None, country: str | None) -> None:
            async with semaphore:
                results[key] = await check_website(
                    website, country_iso2=country, client=client, cfg=cfg
                )

        await asyncio.gather(*(one(*item) for item in items))
    return results


def check_pending(limit: int | None = None, *, recheck: bool = False) -> dict[str, int]:
    """Check websites of leads never checked (or checked > recheck_after_days ago).

    TODO(build step 4):
      1. Select leads where website_checked_at IS NULL (or stale when ``recheck``),
         status active, not sanctions_hit; ``limit`` rows.
      2. Group by domain so each domain is checked once (several leads may share one).
      3. ``asyncio.run(check_many(...))`` with settings.batch.website_concurrency.
      4. Update: website_status, website_checked_at, email (only if empty), whatsapp_number
         + whatsapp_evidence (first hit), socials (merge), and pass copyright_year to the
         age resolver as extra evidence if desired.
      5. Return counts per status.
    """
    raise NotImplementedError("website.check_pending (build step 4)")
