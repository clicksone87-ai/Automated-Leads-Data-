"""Outreach rules: draft priority, templates, opt-out and bounce detection, sanctions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from leads.models import WebsiteStatus
from leads.outreach.gmail_drafts import build_mime, draft_priority
from leads.outreach.gmail_sync import followup_due_at, is_hard_bounce, is_opt_out
from leads.outreach.templates import (
    MissingPlaceholderError,
    build_context,
    language_for_country,
    render,
    template_path,
)
from leads.pipeline.sanctions import SdnEntry, match_sanctions
from leads.settings import SenderSettings


def test_draft_priority_order() -> None:
    order = sorted(
        [WebsiteStatus.WORKING, WebsiteStatus.PARKED, WebsiteStatus.NONE, WebsiteStatus.BROKEN],
        key=draft_priority,
    )
    assert order == [
        WebsiteStatus.NONE,
        WebsiteStatus.BROKEN,
        WebsiteStatus.PARKED,
        WebsiteStatus.WORKING,
    ]
    assert draft_priority(None) > draft_priority(WebsiteStatus.WORKING)


def _ctx(status: str | None, domain: str | None = None) -> dict[str, object]:
    return build_context(
        business_name="Rosie's Bakery",
        category="Bakery",
        city="Leeds",
        website_status=status,
        domain=domain,
        sender=SenderSettings(sender_name="Sam", company="WebCo", company_website="https://webco.test",
                              company_phone="+44 20 0000 0000", company_address="1 Street"),
        delivery_days=7,
        price="£499",
    )  # fmt: skip


class TestTemplates:
    def test_first_email_no_website(self) -> None:
        msg = render("email_first", _ctx(WebsiteStatus.NONE))
        assert msg.subject == "Quick idea for Rosie's Bakery"
        assert "I noticed you don't have a website yet." in msg.body
        assert "bakery businesses in Leeds" in msg.body
        assert 'reply "no thanks"' in msg.body
        assert "{" not in msg.body

    def test_first_email_broken_website(self) -> None:
        msg = render("email_first", _ctx(WebsiteStatus.PARKED, "rosiesbakery.co.uk"))
        assert "your website at rosiesbakery.co.uk isn't loading right now" in msg.body

    def test_followup_and_whatsapp(self) -> None:
        assert render("email_followup", _ctx(WebsiteStatus.NONE)).subject is None
        wa = render(
            "whatsapp_after_reply", {**_ctx(WebsiteStatus.NONE), "link": "https://x.test/m"}
        )
        assert "https://x.test/m" in wa.body

    def test_missing_placeholder_raises(self) -> None:
        with pytest.raises(MissingPlaceholderError):
            render("email_first", {"business_name": "X"})

    def test_language_fallback_to_english(self) -> None:
        assert language_for_country("FR") == "fr"
        assert language_for_country("BR") == "pt"
        assert language_for_country("GB") == "en"
        # No translated template yet -> English file.
        assert template_path("email_first", "fr").parent.name == "templates"

    def test_translated_template_used_when_present(self, tmp_path: Path) -> None:
        (tmp_path / "i18n" / "fr").mkdir(parents=True)
        (tmp_path / "email_first.txt").write_text("Subject: en\n\nen", encoding="utf-8")
        (tmp_path / "i18n" / "fr" / "email_first.txt").write_text(
            "Subject: Bonjour {business_name}\n\nfr", encoding="utf-8"
        )
        assert (
            render("email_first", {"business_name": "X"}, "fr", base=tmp_path).subject
            == "Bonjour X"
        )

    def test_mime(self) -> None:
        msg = build_mime("hello@rosiesbakery.co.uk", "Hi", "Body")
        assert msg["To"] == "hello@rosiesbakery.co.uk"
        assert "From" not in msg


class TestOptOut:
    @pytest.mark.parametrize(
        "text",
        [
            "No thanks.",
            "Please UNSUBSCRIBE me",
            "stop",
            "Remove me from your list",
            "Non merci",
            "Nein danke, kein Interesse",
            "No gracias",
            "нет, спасибо",
            "لا شكرا",
            "Hayır teşekkürler",
        ],
    )
    def test_opt_out(self, text: str) -> None:
        assert is_opt_out(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Yes please, send the mock-up!",
            "Sounds interesting, what does it cost?",
            'Thanks!\n\nOn Mon, 1 Jan 2026 Sam wrote:\n> reply "no thanks" and we won\'t contact you',
            '> If you\'d rather not hear from us, reply "no thanks"\nGreat, call me',
            "We're nonstop busy but interested",
        ],
    )
    def test_not_opt_out(self, text: str) -> None:
        assert not is_opt_out(text)

    def test_hard_bounce(self) -> None:
        assert is_hard_bounce("Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                              "Delivery Status Notification (Failure)", "550 5.1.1 The email account does not exist")  # fmt: skip
        assert not is_hard_bounce(
            "hello@rosiesbakery.co.uk", "Re: Quick idea", "address not found?"
        )

    def test_followup_due(self) -> None:
        sent = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        assert followup_due_at(sent, 6) == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


class TestSanctions:
    ENTRIES = [
        SdnEntry("1", "ACME TRADING LLC", "acme trading", "Entity", {"AE"}),
        SdnEntry("2", "GLOBAL SHIPPING CO", "global shipping", "Entity", set()),
    ]

    def test_same_country_match(self) -> None:
        hit = match_sanctions("Acme Trading L.L.C.", "AE", self.ENTRIES)
        assert hit is not None and hit.ent_num == "1" and hit.score >= 92

    def test_other_country_no_match(self) -> None:
        assert match_sanctions("Acme Trading Ltd", "GB", self.ENTRIES) is None

    def test_countryless_entry_matches_anywhere(self) -> None:
        assert match_sanctions("Global Shipping Company", "GB", self.ENTRIES) is not None

    def test_different_name(self) -> None:
        assert match_sanctions("Rosie's Bakery", "AE", self.ENTRIES) is None
