from __future__ import annotations

import asyncio
from typing import Any

import httpcore
import httpx
import pytest

from leads.enrich import website as w
from leads.enrich.check import changes, email_site, social_site, usable_email
from leads.enrich.safe_http import PublicOnlyBackend, is_public
from leads.enrich.website import Contacts, WebsiteCheck, WhatsAppHit, check_website
from leads.models import Lead, WebsiteStatus
from leads.settings import WebsiteSettings

CFG = WebsiteSettings(retry_wait_seconds=0)
GOOD = (
    "<html><title>Acme</title><body>" + "Acme Garage, welcome to our shop. " * 10 + "</body></html>"
)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "0.0.0.0", "::1",
     "fc00::1", "fe80::1%en0", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::", "100.64.0.1"],
)  # fmt: skip
def test_private_addresses_are_not_public(address: str) -> None:
    assert not is_public(address)


def test_public_addresses() -> None:
    assert is_public("8.8.8.8")
    assert is_public("2606:4700:4700::1111")


@pytest.mark.parametrize(("host", "port"), [("127.0.0.1", 80), ("8.8.8.8", 5432)])
def test_backend_refuses(host: str, port: int) -> None:
    with pytest.raises(httpcore.ConnectError, match="blocked"):
        asyncio.run(PublicOnlyBackend().connect_tcp(host, port, 1.0))


def run(
    handler: Any, url: str, monkeypatch: pytest.MonkeyPatch, dns: bool | None = True
) -> WebsiteCheck:
    async def fake_dns(_: str) -> tuple[bool | None, tuple[str, ...]]:
        return dns, ()

    monkeypatch.setattr(w, "resolve_dns", fake_dns)

    async def go() -> WebsiteCheck:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_website(url, country_iso2="GB", client=client, cfg=CFG)

    return asyncio.run(go())


def respond(code: int, html: str = GOOD, **headers: str) -> httpx.Response:
    return httpx.Response(code, html=html, headers=headers)


def test_working_site(monkeypatch: pytest.MonkeyPatch) -> None:
    check = run(lambda r: respond(200), "https://acme.ie", monkeypatch)
    assert check.status == WebsiteStatus.WORKING


def test_error_that_goes_away_is_not_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(r.url))
        return respond(500 if len(seen) == 1 else 200)

    assert run(handler, "https://acme.ie", monkeypatch).status == WebsiteStatus.WORKING


def test_error_seen_twice_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    check = run(lambda r: respond(500), "https://acme.ie", monkeypatch)
    assert (check.status, check.reason) == (WebsiteStatus.BROKEN, "http 500")


def test_old_deep_link_falls_back_to_the_front_page(monkeypatch: pytest.MonkeyPatch) -> None:
    check = run(
        lambda r: respond(200 if r.url.path == "/" else 404), "https://acme.ie/old", monkeypatch
    )
    assert check.status == WebsiteStatus.WORKING


def test_missing_name_is_broken_but_failed_lookup_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = lambda r: respond(200)  # noqa: E731
    assert run(ok, "https://acme.ie", monkeypatch, dns=False).status == WebsiteStatus.BROKEN
    assert run(ok, "https://acme.ie", monkeypatch, dns=None).status is None


def test_timeouts_prove_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    assert run(handler, "https://acme.ie", monkeypatch).status is None


def test_social_redirect_is_not_visited(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        assert "facebook" not in r.url.host
        return httpx.Response(302, headers={"location": "https://www.facebook.com/acme"})

    assert run(handler, "https://acme.ie", monkeypatch).status == WebsiteStatus.REDIRECT_SOCIAL


def test_robots_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /")

    assert run(handler, "https://acme.ie", monkeypatch).status is None


def lead(**kw: Any) -> Lead:
    base: dict[str, Any] = {"lead_id": "a", "name": "Acme Garage", "socials": {}}
    return Lead(**(base | kw))


def test_email_site() -> None:
    assert email_site("info@acme.ie") == "https://acme.ie"
    assert email_site("acme@gmail.com") is None
    assert email_site("acme@hotmail.co.uk") is None
    assert email_site(None) is None


def test_guessed_site_must_show_the_business() -> None:
    check = WebsiteCheck(
        WebsiteStatus.WORKING, "ok", final_url="https://acme.ie/", text="Other Ltd"
    )
    values = changes(lead(email="x@acme.ie"), check, guessed=True, shared=False)
    assert values["website_status"] is None and "website" not in values
    check.text = "Welcome to ACME Garage!"
    values = changes(lead(email="x@acme.ie"), check, guessed=True, shared=False)
    assert values["website_status"] == WebsiteStatus.WORKING
    assert values["domain"] == "acme.ie"


def test_guessed_site_that_errors_is_not_called_missing() -> None:
    check = WebsiteCheck(WebsiteStatus.BROKEN, "http 500")
    assert changes(lead(), check, guessed=True, shared=False)["website_status"] is None
    check = WebsiteCheck(WebsiteStatus.BROKEN, str(w.FetchError.DNS))
    assert changes(lead(), check, guessed=True, shared=False)["website_status"] == "none"


def test_contacts_are_taken_only_from_a_site_of_its_own() -> None:
    contacts = Contacts(
        emails=["designer@agency.com", "hi@acme.ie"],
        whatsapp=[WhatsAppHit("+353861234567", "https://acme.ie/", "link")],
    )
    check = WebsiteCheck(WebsiteStatus.WORKING, "ok", "https://acme.ie/", contacts)
    assert usable_email(check) == "hi@acme.ie"
    own = changes(lead(website="https://acme.ie"), check, guessed=False, shared=False)
    assert (own["email"], own["whatsapp_number"]) == ("hi@acme.ie", "+353861234567")
    chain = changes(lead(website="https://acme.ie"), check, guessed=False, shared=True)
    assert "email" not in chain
    known = changes(
        lead(website="https://acme.ie", email="me@acme.ie"), check, guessed=False, shared=False
    )
    assert "email" not in known


def test_no_website_listed_is_stored_as_none() -> None:
    check = WebsiteCheck(WebsiteStatus.NONE, "no website listed")
    assert changes(lead(), check, guessed=True, shared=False)["website_status"] == "none"


def test_social_site_from_profile_name() -> None:
    assert social_site({"instagram": "https://www.instagram.com/acme.garage/"}) == (
        "https://acmegarage.com"
    )
    assert social_site({"x": "https://twitter.com/AcmeGarage"}) == "https://acmegarage.com"
    assert social_site({"youtube": "https://www.youtube.com/@acme_garage"}) == (
        "https://acmegarage.com"
    )
    assert social_site({"facebook": "https://www.facebook.com/539574029505304"}) is None
    assert social_site({"instagram": "https://www.instagram.com/explore/"}) is None
    assert social_site({"instagram": "https://www.instagram.com/p/"}) is None
    assert social_site(None) is None


def test_social_guess_needs_a_link_back_or_same_contact() -> None:
    own = lead(
        socials={"instagram": "https://instagram.com/acme.garage"}, phone_e164="+447700900123"
    )
    site = WebsiteCheck(
        WebsiteStatus.WORKING, "ok", final_url="https://acmegarage.com/", text="Acme Garage Leeds"
    )
    # Same name is not enough: anyone can own the .com of a profile name.
    values = changes(own, site, guessed=True, shared=False, via="social")
    assert values["website_status"] == "none" and "website" not in values

    site.contacts = Contacts(socials={"instagram": "https://www.instagram.com/acme.garage/"})
    values = changes(own, site, guessed=True, shared=False, via="social")
    assert values["domain"] == "acmegarage.com"
    assert values["website_reason"] == "found through a social profile name"

    by_phone = WebsiteCheck(
        WebsiteStatus.WORKING, "ok", final_url="https://acmegarage.com/", text="Call 07700 900123"
    )
    assert changes(own, by_phone, guessed=True, shared=False, via="social")["website_status"] == (
        "working"
    )

    broken = WebsiteCheck(WebsiteStatus.BROKEN, "http 500")
    assert changes(own, broken, guessed=True, shared=False, via="social")["website_status"] == (
        "none"
    )


class FakeBrowser:
    def __init__(self, html: str | None) -> None:
        self.html = html
        self.visited: list[str] = []

    async def render(self, url: str) -> dict[str, Any] | None:
        self.visited.append(url)
        if self.html is None:
            return None
        return {"final_url": url, "status_code": 200, "html": self.html}


def run_with_browser(
    html: str, browser: FakeBrowser, monkeypatch: pytest.MonkeyPatch
) -> WebsiteCheck:
    async def fake_dns(_: str) -> tuple[bool | None, tuple[str, ...]]:
        return True, ()

    monkeypatch.setattr(w, "resolve_dns", fake_dns)

    async def go() -> WebsiteCheck:
        transport = httpx.MockTransport(lambda r: respond(200, html))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_website(
                "https://acme.ie",
                country_iso2="GB",
                client=client,
                cfg=CFG,
                browser=browser,  # type: ignore[arg-type]
            )

    return asyncio.run(go())


SCRIPT_ONLY = (
    "<html><head><script src='/app.js'></script></head><body><div id=root></div></body></html>"
)
RENDERED = (
    "<html><head><title>Acme</title></head><body><h1>Acme Garage</h1>"
    "<p>Car repairs in Dublin since 2019. Call us or email hello@acme.ie for a quote today, "
    "we are open six days a week.</p></body></html>"
)


def test_script_only_page_is_read_in_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = FakeBrowser(RENDERED)
    check = run_with_browser(SCRIPT_ONLY, browser, monkeypatch)
    assert browser.visited == ["https://acme.ie"]
    assert check.status == WebsiteStatus.WORKING
    assert check.reason == "200 with content, in a browser"
    assert "hello@acme.ie" in check.contacts.emails


def test_browser_is_not_used_for_readable_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = FakeBrowser(RENDERED)
    assert run_with_browser(GOOD, browser, monkeypatch).status == WebsiteStatus.WORKING
    assert browser.visited == []


def test_page_the_browser_cannot_open_keeps_its_first_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = run_with_browser(SCRIPT_ONLY, FakeBrowser(None), monkeypatch)
    assert check.status is None and check.reason == "200, content not readable"
