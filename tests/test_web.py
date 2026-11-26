from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import leads.db as db
from leads.models import Base, Lead, Suppression
from leads.settings import Settings
from leads.web import queries
from leads.web.app import app, csv_cell, normalise_contact

URL = os.environ.get("TEST_DATABASE_URL")


def test_page_and_headers() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_other_hosts_are_refused() -> None:
    assert TestClient(app).get("/", headers={"Host": "evil.example"}).status_code == 400


def test_cross_site_post_is_refused() -> None:
    response = TestClient(app).post("/api/suppression", json={"contact": "a@example.com"})
    assert response.status_code == 403


def test_csv_cells_cannot_be_formulas() -> None:
    assert csv_cell("=1+1") == "'=1+1"
    assert csv_cell("+447700900123") == "+447700900123"
    assert csv_cell(["a", "b"]) == "a;b"
    assert csv_cell(None) == ""


def test_normalise_contact() -> None:
    assert normalise_contact(" Info@Example.COM ") == "info@example.com"
    assert normalise_contact("+44 20 7946 0018") == "+442079460018"
    assert normalise_contact("02079460018") is None
    assert normalise_contact("hello") is None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    assert URL.rsplit("/", 1)[-1].endswith("_test"), "refusing to wipe a non-test database"
    settings = Settings(database_url=URL)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    queries.clear_cache()
    with db.get_engine().begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
    with db.session_scope() as s:
        s.add_all(
            [
                Lead(lead_id="a", name="100% Cafe", country_iso2="GB", city="Leeds",
                     email="cafe@example.com", created_date=date(2020, 5, 1),
                     category_group="food_drink", sources=["overture"]),
                Lead(lead_id="b", name="Bob Shop", country_iso2="IE", website="https://bob.ie",
                     domain="bob.ie", created_date=date(2026, 1, 1), age_unknown=True,
                     sources=["foursquare"]),
                Lead(lead_id="c", name="Closed", country_iso2="GB", status="closed"),
                Lead(lead_id="d", name="Sanctioned", country_iso2="GB", sanctions_hit=True),
            ]
        )  # fmt: skip
    yield TestClient(app)
    db.get_engine().dispose()
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    queries.clear_cache()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", {"a", "b"}),
        ("q=100%25", {"a"}),
        ("q=_ob", set()),
        ("country=ie", {"b"}),
        ("year=2020", {"a"}),
        ("year=2026", set()),
        ("age=unknown", {"b"}),
        ("email=yes", {"a"}),
        ("website=not_listed", {"a"}),
        ("source=foursquare", {"b"}),
    ],
)
def test_lead_filters(client: TestClient, query: str, expected: set[str]) -> None:
    data = client.get(f"/api/leads?{query}").json()
    assert {r["lead_id"] for r in data["leads"]} == expected
    assert data["total"] == len(expected)


def test_first_seen_is_not_shown_as_opening_date(client: TestClient) -> None:
    assert client.get("/api/leads/b").json()["created_date"] is None
    assert "2026-01-01" not in client.get("/api/leads.csv?age=unknown").text


def test_known_dates_sort_before_first_seen_ones(client: TestClient) -> None:
    leads = client.get("/api/leads?sort=newest").json()["leads"]
    assert [r["lead_id"] for r in leads] == ["a", "b"]


def test_email_needs_a_checked_website(client: TestClient) -> None:
    assert any("not been checked" in b for b in client.get("/api/leads/a/email").json()["blockers"])
    response = client.post("/api/leads/a/email", headers={"X-Requested-With": "leads"})
    assert response.status_code == 409
    assert client.get("/api/leads/zzz/email").status_code == 404


def test_overview_and_setup(client: TestClient) -> None:
    totals = client.get("/api/overview").json()["totals"]
    assert (totals["total"], totals["dated"], totals["best"]) == (2, 1, 1)
    assert client.get("/api/setup").json()["checked"] == 0
    assert client.get("/api/options").json()["years"] == [2020]


def test_opt_out_is_stored_once(client: TestClient) -> None:
    for _ in range(2):
        response = client.post(
            "/api/suppression",
            json={"contact": "Cafe@example.com"},
            headers={"X-Requested-With": "leads"},
        )
        assert response.status_code == 201
    with db.session_scope() as s:
        assert s.query(Suppression).count() == 1
    assert client.get("/api/leads/a").json()["suppressed"] is True


def _whatsapp_leads() -> None:
    from datetime import UTC, datetime

    from leads.models import Outreach

    with db.session_scope() as s:
        s.add_all(
            [
                Lead(lead_id="w1", name="Replied Cafe", country_iso2="GB",
                     email="w1@example.com", phone_e164="+447700900123", phone_type="mobile"),
                Lead(lead_id="w2", name="Shows WhatsApp", country_iso2="GB",
                     whatsapp_number="+447700900456"),
                Lead(lead_id="w3", name="Landline Only", country_iso2="GB",
                     email="w3@example.com", phone_e164="+442079460018", phone_type="landline"),
            ]
        )  # fmt: skip
        s.flush()
        now = datetime.now(UTC)
        s.add_all(
            [
                Outreach(lead_id="w1", stage="replied", recipient="w1@example.com",
                         drafted_at=now, sent_at=now, replied_at=now),
                Outreach(lead_id="w3", stage="replied", recipient="w3@example.com",
                         drafted_at=now, sent_at=now, replied_at=now),
            ]
        )  # fmt: skip


def test_whatsapp_only_after_reply_or_public_number(client: TestClient) -> None:
    _whatsapp_leads()
    cold = client.get("/api/leads/a/whatsapp").json()
    assert cold["blockers"] and "replied" in cold["blockers"][0]

    replied = client.get("/api/leads/w1/whatsapp").json()
    assert replied["blockers"] == []
    assert replied["number"] == "+447700900123"
    assert "Thanks for getting back to me about Replied Cafe" in replied["text"]

    public = client.get("/api/leads/w2/whatsapp").json()
    assert public["blockers"] == []
    assert "welcome messages on WhatsApp" in public["text"]

    landline = client.get("/api/leads/w3/whatsapp").json()
    assert landline["blockers"] == ["There is no mobile or WhatsApp number for this business."]


def test_whatsapp_opened_is_recorded_once(client: TestClient) -> None:
    _whatsapp_leads()
    headers = {"X-Requested-With": "leads"}
    first = client.post("/api/leads/w2/whatsapp", headers=headers)
    assert first.status_code == 201
    again = client.post("/api/leads/w2/whatsapp", headers=headers)
    assert again.json()["opened_at"] == first.json()["opened_at"]
    assert client.get("/api/leads/w2/whatsapp").json()["opened_at"] is not None
    assert client.post("/api/leads/a/whatsapp", headers=headers).status_code == 409


def test_whatsapp_respects_opt_out(client: TestClient) -> None:
    _whatsapp_leads()
    with db.session_scope() as s:
        s.add(Suppression(email_or_phone="+447700900456", reason="manual"))
    assert "This contact has opted out." in client.get("/api/leads/w2/whatsapp").json()["blockers"]
