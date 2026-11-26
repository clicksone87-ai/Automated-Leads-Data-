"""Website checker.

Classifies each lead's website as:

* ``working``         HTTP 200 with real content
* ``broken``          name does not exist, 404/410/5xx, bad certificate, empty page (seen twice)
* ``parked``          parking/marketplace host or nameserver, or phrases like "domain for sale"
* ``redirect_social`` the site is (or redirects to) only a social media page
* ``none``            no website at all

or leaves the status empty when the check proves nothing (slow network, a server that blocks
automated visits). A wrong "your website is down" is worse than no answer.

It also extracts public emails, publicly shown WhatsApp numbers (with the URL of the page
showing them), social profile links and the earliest copyright year.

Politeness rules: only the home page and one contact page are fetched (redirects and
one retry aside, at most ``max_requests_per_domain`` pages), robots.txt is respected, 12 s timeout,
the User-Agent names the tool. WhatsApp numbers are only read from the page; no
WhatsApp API is ever called.
"""

from __future__ import annotations

import asyncio
import re
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx
import structlog
from selectolax.lexbor import LexborHTMLParser

from leads.enrich import browser as headless
from leads.enrich import parked
from leads.enrich.safe_http import PublicOnlyTransport, read_limited
from leads.models import WebsiteStatus
from leads.pipeline.normalise import (
    SOCIAL_DOMAINS,
    host_of,
    normalise_phone,
    normalise_website,
    registered_domain,
)
from leads.settings import WebsiteSettings

log = structlog.get_logger(__name__)


class FetchError(StrEnum):
    DNS = "domain name does not exist"
    DNS_UNSURE = "dns lookup failed"
    TIMEOUT = "timeout"
    SSL = "invalid security certificate"
    SSL_UNSURE = "security certificate could not be verified"
    BLOCKED = "blocked address"
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
    is_html: bool = True


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
    status: WebsiteStatus | None  # None = could not tell
    reason: str
    final_url: str | None = None
    contacts: Contacts = field(default_factory=Contacts)
    text: str = ""  # visible text, to recognise whose site it is
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


# Only these show that ordinary visitors cannot use the site. 401/403/429/503 are usually
# a server turning automated visits away.
_BROKEN_CODES = frozenset({404, 410, 500, 502, 504, 521, 522, 523, 525, 526})
_PROOF_OF_BROKEN = frozenset({FetchError.DNS, FetchError.SSL, FetchError.TOO_MANY_REDIRECTS})
_RETRY = frozenset({FetchError.TIMEOUT, FetchError.CONNECT, FetchError.SSL, FetchError.SSL_UNSURE})

# --- classification ---------------------------------------------------------------------


def classify(
    website: str | None,
    home: PageFetch | None,
    *,
    nameservers: tuple[str, ...] = (),
    min_text_chars: int = 100,
) -> tuple[WebsiteStatus | None, str]:
    """Classify a website from its home-page fetch. Pure function (no network).

    The status is None when the fetch does not show whether visitors can use the site.
    """
    if not website or not website.strip():
        return WebsiteStatus.NONE, "no website"
    site_domain = registered_domain(host_of(website))
    if site_domain in SOCIAL_DOMAINS:
        return WebsiteStatus.REDIRECT_SOCIAL, "website is a social profile"
    if parked.has_parked_nameservers(nameservers):
        return WebsiteStatus.PARKED, "parking nameservers"
    if home is None:
        return None, "not fetched"
    if home.error is not None:
        return (WebsiteStatus.BROKEN if home.error in _PROOF_OF_BROKEN else None), str(home.error)

    final_host = host_of(home.final_url or home.requested_url)
    if registered_domain(final_host) in SOCIAL_DOMAINS:
        return WebsiteStatus.REDIRECT_SOCIAL, f"redirects to {final_host}"
    if parked.is_parked_host(final_host):
        return WebsiteStatus.PARKED, f"parking host {final_host}"
    if home.status_code is None:
        return None, "no response"
    if home.status_code >= 400:
        broken = home.status_code in _BROKEN_CODES
        return (WebsiteStatus.BROKEN if broken else None), f"http {home.status_code}"
    if not home.is_html:
        return WebsiteStatus.WORKING, "200, a file"

    text = visible_text(home.html)
    title_node = parse_html(home.html).css_first("title") if home.html else None
    title = title_node.text(strip=True) if title_node is not None else ""
    if parked.has_parked_phrases(f"{title} {text}"):
        return WebsiteStatus.PARKED, "parked phrases"
    if len(text) >= min_text_chars:
        return WebsiteStatus.WORKING, "200 with content"
    # JS-rendered sites (Wix/Squarespace/React) have little server-side text but load scripts.
    lowered = (home.html or "").lower()
    if title and "<script" in lowered:
        return WebsiteStatus.WORKING, "200, JS-rendered"
    if any(tag in lowered for tag in ("<script", "<iframe", "<frame", "<img", "http-equiv")):
        return None, "200, content not readable"
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


async def resolve_dns(domain: str, timeout: float = 8.0) -> tuple[bool | None, tuple[str, ...]]:
    """(resolves?, nameservers) for a domain. None = the lookup itself failed."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout
    ok: bool | None = None
    for rtype in ("A", "AAAA"):
        try:
            await resolver.resolve(domain, rtype)
            ok = True
            break
        except dns.resolver.NXDOMAIN:
            ok = False
            break
        except dns.exception.DNSException:
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


def _ssl_error(message: str) -> FetchError:
    """A missing intermediate certificate is repaired by browsers, so it proves nothing."""
    proof = ("certificate has expired", "hostname mismatch", "self-signed", "self signed")
    return FetchError.SSL if any(p in message.lower() for p in proof) else FetchError.SSL_UNSURE


MAX_REDIRECTS = 8
TOTAL_SECONDS_FACTOR = 3  # whole page, against servers that trickle bytes


async def _request(client: httpx.AsyncClient, url: str, result: PageFetch) -> None:
    for _ in range(MAX_REDIRECTS + 1):
        result.final_url = url
        # Social networks do not allow automated visits; landing there is answer enough.
        if registered_domain(host_of(url)) in SOCIAL_DOMAINS:
            result.status_code = 200
            return
        async with client.stream("GET", url, follow_redirects=False) as response:
            result.status_code = response.status_code
            target = response.headers.get("location")
            if response.is_redirect and target:
                url = urljoin(url, target)
                if not url.lower().startswith(("http://", "https://")):
                    result.error = FetchError.CONNECT
                    return
                continue
            content_type = response.headers.get("content-type", "")
            result.is_html = "html" in content_type or not content_type
            if result.is_html:
                result.html = await read_limited(response)
            return
    result.error = FetchError.TOO_MANY_REDIRECTS


async def fetch_page(client: httpx.AsyncClient, url: str) -> PageFetch:
    result = PageFetch(requested_url=url)
    total = (client.timeout.read or 12.0) * TOTAL_SECONDS_FACTOR
    try:
        async with asyncio.timeout(total):
            await _request(client, url, result)
    except (httpx.TimeoutException, TimeoutError):
        result.error = FetchError.TIMEOUT
    except httpx.ConnectError as exc:
        message = str(exc)
        if "blocked" in message:
            result.error = FetchError.BLOCKED
        elif "certificate" in message.lower():
            result.error = _ssl_error(message)
        else:
            result.error = FetchError.CONNECT
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        result.error = FetchError.CONNECT
    return result


async def _robots(client: httpx.AsyncClient, base_url: str) -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser()
    page = PageFetch(urljoin(base_url, "/robots.txt"))
    lines: list[str] = []
    try:
        async with asyncio.timeout((client.timeout.read or 12.0) * 2):
            async with client.stream("GET", page.requested_url) as response:
                if response.status_code == 200 and "html" not in response.headers.get(
                    "content-type", ""
                ):
                    lines = (await read_limited(response, 500_000)).splitlines()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TimeoutError):
        pass
    parser.parse(lines)  # empty -> everything allowed
    return parser


def make_client(cfg: WebsiteSettings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=cfg.timeout_seconds,
        follow_redirects=True,
        max_redirects=5,
        transport=PublicOnlyTransport(),
        trust_env=False,
        headers={"User-Agent": cfg.user_agent, "Accept": "text/html,*/*;q=0.5"},
    )


def _root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


# Reasons from classify() for a page whose text only scripts can show.
NEEDS_BROWSER = frozenset({"200, JS-rendered", "200, content not readable"})


async def check_website(
    website: str | None,
    *,
    country_iso2: str | None,
    client: httpx.AsyncClient,
    cfg: WebsiteSettings,
    browser: headless.Browser | None = None,
) -> WebsiteCheck:
    """Check one website: DNS, robots.txt, home page, then (if working) one contact page."""
    url = normalise_website(website)
    if url is None:
        return WebsiteCheck(WebsiteStatus.NONE, "no website")
    status, reason = classify(url, PageFetch(url))  # catches social-profile websites
    if status == WebsiteStatus.REDIRECT_SOCIAL:
        return WebsiteCheck(status, reason, final_url=url)

    host = urlsplit(url).hostname or ""
    resolves, nameservers = await resolve_dns(host)
    if resolves is False:
        # Some sites exist only with, or only without, "www.".
        other = host[4:] if host.startswith("www.") else f"www.{host}"
        other_resolves, nameservers = await resolve_dns(other)
        if other_resolves is False:
            return WebsiteCheck(WebsiteStatus.BROKEN, str(FetchError.DNS))
        if other_resolves:
            url = url.replace(f"//{host}", f"//{other}", 1)
        resolves = other_resolves
    if resolves is None:
        return WebsiteCheck(None, str(FetchError.DNS_UNSURE))

    robots = await _robots(client, url) if cfg.respect_robots_txt else None

    def allowed(target: str) -> bool:
        return robots is None or robots.can_fetch(cfg.user_agent, target)

    if not allowed(url):
        home = PageFetch(url, error=FetchError.ROBOTS)
        status, reason = classify(url, home, nameservers=nameservers)
        return WebsiteCheck(status, reason)

    async def look(target: str) -> tuple[PageFetch, WebsiteStatus | None, str]:
        page = await fetch_page(client, target)
        return page, *classify(
            target, page, nameservers=nameservers, min_text_chars=cfg.min_text_chars
        )

    home, status, reason = await look(url)
    if status == WebsiteStatus.BROKEN or home.error in _RETRY:
        # Nothing is called broken on one visit. An old deep link may be gone while the
        # site itself is fine, so a missing page is asked for again at the front page.
        retry = _root(url) if home.status_code in (404, 410) else url
        if home.error in _RETRY and url.startswith("https://"):
            plain = "http://" + url.removeprefix("https://")
            again = await look(plain)
            if again[1] == WebsiteStatus.WORKING:
                home, status, reason = again
        if status != WebsiteStatus.WORKING and allowed(retry):
            await asyncio.sleep(cfg.retry_wait_seconds)
            first = status
            home, status, reason = await look(retry)
            if first is None and status == WebsiteStatus.BROKEN:
                status = None  # only seen once

    if browser is not None and reason in NEEDS_BROWSER and allowed(home.final_url or url):
        rendered = await browser.render(home.final_url or url)
        if rendered is not None:
            page = PageFetch(url, rendered["final_url"], rendered["status_code"], rendered["html"])
            shown, why = classify(
                url, page, nameservers=nameservers, min_text_chars=cfg.min_text_chars
            )
            if shown in (
                WebsiteStatus.WORKING,
                WebsiteStatus.PARKED,
                WebsiteStatus.REDIRECT_SOCIAL,
            ):
                home, status, reason = page, shown, f"{why}, in a browser"

    check = WebsiteCheck(status, reason, final_url=home.final_url)
    if status != WebsiteStatus.WORKING or not home.html:
        return check

    page_url = home.final_url or url
    contacts = [extract_contacts(home.html, page_url, country_iso2)]
    check.text = visible_text(home.html)
    contact_url = find_contact_url(home.html, page_url)
    if contact_url and cfg.max_requests_per_domain > 1 and allowed(contact_url):
        contact_page = await fetch_page(client, contact_url)
        if contact_page.html and (contact_page.status_code or 500) < 400:
            contacts.append(
                extract_contacts(
                    contact_page.html, contact_page.final_url or contact_url, country_iso2
                )
            )
            check.text += " " + visible_text(contact_page.html)
    check.contacts = merge_contacts(*contacts)
    return check


async def check_many(
    items: list[tuple[str, str | None, str | None]], cfg: WebsiteSettings, concurrency: int = 50
) -> dict[str, WebsiteCheck]:
    """Check many websites concurrently. ``items`` = (key, website, country_iso2).

    Sites on one host are visited one after another.
    """
    semaphore = asyncio.Semaphore(concurrency)
    hosts: dict[str, asyncio.Lock] = {}
    results: dict[str, WebsiteCheck] = {}
    browser: headless.Browser | None = None
    if cfg.browser_fallback and items and headless.available():
        try:
            browser = await headless.Browser(cfg).__aenter__()
        except Exception as exc:  # e.g. Chromium not downloaded: check without it
            log.warning("browser_unavailable", error=repr(exc))
    async with make_client(cfg) as client:

        async def one(key: str, website: str | None, country: str | None) -> None:
            lock = hosts.setdefault(host_of(website) or key, asyncio.Lock())
            async with lock, semaphore:
                try:
                    results[key] = await check_website(
                        website, country_iso2=country, client=client, cfg=cfg, browser=browser
                    )
                except Exception as exc:  # one odd site must not lose the batch
                    log.warning("website_check_failed", website=website, error=repr(exc))
                    results[key] = WebsiteCheck(None, "check failed")

        try:
            await asyncio.gather(*(one(*item) for item in items))
        finally:
            if browser is not None:
                await browser.__aexit__(None, None, None)
    return results


_CONTROL_URLS = ("https://www.wikipedia.org/", "https://www.gov.uk/", "https://www.iana.org/")


async def network_ok(cfg: WebsiteSettings) -> bool:
    """Whether this machine can reach well-known sites; otherwise results mean nothing."""
    async with make_client(cfg) as client:
        pages = await asyncio.gather(*(fetch_page(client, url) for url in _CONTROL_URLS))
    return sum(p.status_code is not None and p.error is None for p in pages) >= 2
