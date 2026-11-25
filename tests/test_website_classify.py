from __future__ import annotations

from datetime import date

import pytest

from leads.enrich.website import (
    FetchError,
    PageFetch,
    classify,
    extract_contacts,
    extract_copyright_year,
    extract_emails,
    extract_socials,
    extract_whatsapp,
    find_contact_url,
    merge_contacts,
)
from leads.models import WebsiteStatus

SITE = "https://rosiesbakery.co.uk"


def page(html: str | None, status: int = 200, final_url: str = SITE + "/") -> PageFetch:
    return PageFetch(requested_url=SITE, final_url=final_url, status_code=status, html=html)


class TestClassify:
    def test_no_website(self) -> None:
        assert classify(None, None)[0] == WebsiteStatus.NONE
        assert classify("  ", None)[0] == WebsiteStatus.NONE

    def test_working(self, fixture_html) -> None:
        status, _ = classify(SITE, page(fixture_html("working_home.html")))
        assert status == WebsiteStatus.WORKING

    @pytest.mark.parametrize(
        "error", [FetchError.DNS, FetchError.SSL, FetchError.TOO_MANY_REDIRECTS]
    )
    def test_fetch_errors_are_broken(self, error: FetchError) -> None:
        status, reason = classify(SITE, PageFetch(SITE, error=error))
        assert status == WebsiteStatus.BROKEN
        assert reason == str(error)

    @pytest.mark.parametrize("code", [404, 410, 500, 502])
    def test_http_errors_are_broken(self, code: int, fixture_html) -> None:
        assert (
            classify(SITE, page(fixture_html("working_home.html"), status=code))[0]
            == WebsiteStatus.BROKEN
        )

    @pytest.mark.parametrize(
        "error",
        [FetchError.DNS_UNSURE, FetchError.TIMEOUT, FetchError.CONNECT, FetchError.SSL_UNSURE,
         FetchError.ROBOTS, FetchError.BLOCKED],
    )  # fmt: skip
    def test_unclear_errors_prove_nothing(self, error: FetchError) -> None:
        assert classify(SITE, PageFetch(SITE, error=error)) == (None, str(error))

    @pytest.mark.parametrize("code", [401, 403, 429, 503])
    def test_turned_away_visits_prove_nothing(self, code: int) -> None:
        assert classify(SITE, page("<p>Access denied</p>", status=code))[0] is None

    def test_parking_words_on_a_full_page_are_ignored(self) -> None:
        html = "<body>" + "Our spring menu is served all day. " * 40 + "Related searches</body>"
        assert classify(SITE, page(html))[0] == WebsiteStatus.WORKING

    def test_nameserver_hint_needs_the_whole_domain(self) -> None:
        html = "<body>" + "Welcome to our garage. " * 20 + "</body>"
        status, _ = classify(SITE, page(html), nameservers=("ns1.sudan.com",))
        assert status == WebsiteStatus.WORKING

    def test_tiktok_profile_is_social(self) -> None:
        url = "https://www.tiktok.com/@acme"
        assert classify(url, PageFetch(url))[0] == WebsiteStatus.REDIRECT_SOCIAL

    def test_empty_page_is_broken(self, fixture_html) -> None:
        status, reason = classify(SITE, page(fixture_html("empty.html")))
        assert status == WebsiteStatus.BROKEN
        assert "empty" in reason

    def test_js_rendered_page_is_working(self) -> None:
        html = "<html><head><title>Joe's Plumbing</title><script src='/app.js'></script></head><body><div id=root></div></body></html>"
        assert classify(SITE, page(html))[0] == WebsiteStatus.WORKING

    def test_parked_phrases(self, fixture_html) -> None:
        assert classify(SITE, page(fixture_html("parked.html")))[0] == WebsiteStatus.PARKED

    def test_parked_host_redirect(self) -> None:
        fetched = page(
            "<html><body>anything</body></html>",
            final_url="https://www.hugedomains.com/domain_profile.cfm?d=rosiesbakery",
        )
        assert classify(SITE, fetched)[0] == WebsiteStatus.PARKED

    def test_parked_nameservers(self) -> None:
        status, _ = classify(SITE, None, nameservers=("ns1.sedoparking.com", "ns2.sedoparking.com"))
        assert status == WebsiteStatus.PARKED

    def test_redirect_to_facebook(self) -> None:
        fetched = page("<html></html>", final_url="https://www.facebook.com/rosiesbakeryleeds")
        assert classify(SITE, fetched)[0] == WebsiteStatus.REDIRECT_SOCIAL

    def test_website_is_instagram_profile(self) -> None:
        assert (
            classify("https://instagram.com/rosies.bakery", None)[0]
            == WebsiteStatus.REDIRECT_SOCIAL
        )

    def test_linkedin_only_is_social(self) -> None:
        # Brief: redirect_social is only Facebook/Instagram.
        fetched = page("<html></html>", final_url="https://www.linkedin.com/company/rosies")
        assert classify(SITE, fetched)[0] == WebsiteStatus.REDIRECT_SOCIAL


class TestExtract:
    def test_emails(self, fixture_html) -> None:
        emails = extract_emails(fixture_html("working_home.html"))
        assert emails == ["hello@rosiesbakery.co.uk", "orders@rosiesbakery.co.uk"]

    def test_whatsapp_link(self, fixture_html) -> None:
        hits = extract_whatsapp(fixture_html("working_home.html"), SITE + "/", "GB")
        assert [(h.number, h.kind, h.evidence_url) for h in hits] == [
            ("+447400123456", "link", SITE + "/")
        ]

    def test_whatsapp_text_and_api_link(self, fixture_html) -> None:
        url = SITE + "/contact-us"
        hits = extract_whatsapp(fixture_html("contact_page.html"), url, "GB")
        assert {(h.number, h.kind) for h in hits} == {
            ("+447400123456", "link"),
            ("+447400123457", "text"),
        }
        assert all(h.evidence_url == url for h in hits)

    def test_plain_phone_is_not_whatsapp(self) -> None:
        html = "<p>Call us on 07400 123456</p>"
        assert extract_whatsapp(html, SITE, "GB") == []

    def test_socials_skip_share_links(self, fixture_html) -> None:
        socials = extract_socials(fixture_html("working_home.html"))
        assert socials == {
            "facebook": "https://www.facebook.com/rosiesbakeryleeds",
            "instagram": "https://instagram.com/rosies.bakery",
        }

    def test_copyright_year_range(self, fixture_html) -> None:
        assert (
            extract_copyright_year(fixture_html("working_home.html"), today=date(2026, 1, 1))
            == 2019
        )
        assert extract_copyright_year("<p>Copyright 2031</p>", today=date(2026, 1, 1)) is None

    def test_contact_url(self, fixture_html) -> None:
        assert (
            find_contact_url(fixture_html("working_home.html"), SITE + "/") == SITE + "/contact-us"
        )

    def test_merge_contacts(self, fixture_html) -> None:
        home = extract_contacts(fixture_html("working_home.html"), SITE + "/", "GB")
        contact = extract_contacts(fixture_html("contact_page.html"), SITE + "/contact-us", "GB")
        merged = merge_contacts(home, contact)
        assert merged.emails == ["hello@rosiesbakery.co.uk", "orders@rosiesbakery.co.uk"]
        assert [h.number for h in merged.whatsapp] == ["+447400123456", "+447400123457"]
        assert merged.whatsapp[0].evidence_url == SITE + "/"  # first page that showed it
        assert merged.copyright_year == 2019
