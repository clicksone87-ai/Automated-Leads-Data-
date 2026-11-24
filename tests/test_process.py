from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from leads.pipeline.age import AgeResult
from leads.pipeline.match import merge_fields
from leads.pipeline.process import (
    ExistingLead,
    age_evidence,
    build_lead,
    choose_survivor,
    combine_age,
    socials_from_rows,
    to_match_record,
)

NOW = datetime(2026, 11, 24, tzinfo=UTC)
UNKNOWN = AgeResult(created_date=None, age_source=None, age_unknown=True)


def row(source: str, rid: str = "1", **kw: Any) -> dict[str, Any]:
    base = dict.fromkeys(
        ("category", "phone_e164", "phone_type", "email", "website", "domain", "listing_url", "address", "city",
         "region", "postcode", "postcode_key", "lat", "lon", "created_date")
    )  # fmt: skip
    return {
        **base, "source": source, "source_record_id": rid, "name": "Acme", "name_norm": "acme",
        "category_group": "other", "country_iso2": "GB", "shared_ids": [], "raw": {}, **kw,
    }  # fmt: skip


def existing(lead_id: str = "a", **kw: Any) -> ExistingLead:
    values: dict[str, Any] = {
        "first_seen": NOW, "created_date": None, "age_source": None, "age_unknown": True,
        "contacted": False, **kw,
    }  # fmt: skip
    return ExistingLead(lead_id=lead_id, **values)


def test_merge_prefers_trusted_source_and_fills_gaps() -> None:
    merged = merge_fields(
        [
            row("overture", name="acme", phone_e164="+441", phone_type="mobile", city="Leeds"),
            row("companies_house", name="ACME LTD"),
        ]
    )
    assert merged["name"] == "ACME LTD"
    assert (merged["phone_e164"], merged["city"]) == ("+441", "Leeds")
    assert merged["sources"] == ["companies_house", "overture"]


def test_merge_own_site_beats_social_profile() -> None:
    merged = merge_fields(
        [
            row("foursquare", website="https://facebook.com/a", domain="facebook.com"),
            row("overture", website="https://a.example", domain="a.example"),
        ]
    )
    assert merged["domain"] == "a.example"


def test_merge_keeps_phone_and_type_from_one_record() -> None:
    merged = merge_fields(
        [
            row("foursquare", phone_e164="+441", phone_type="landline"),
            row("overture", phone_e164="+442", phone_type="mobile"),
        ]
    )
    assert (merged["phone_e164"], merged["phone_type"]) == ("+441", "landline")


def test_merge_specific_category_beats_other() -> None:
    merged = merge_fields(
        [
            row("companies_house", category="99999", category_group="other"),
            row("overture", category="bakery", category_group="food_drink"),
        ]
    )
    assert (merged["category"], merged["category_group"]) == ("bakery", "food_drink")


def test_social_domain_is_not_a_match_key() -> None:
    assert to_match_record(row("overture", domain="facebook.com")).domain is None
    assert to_match_record(row("overture", domain="a.example")).domain == "a.example"


def test_age_evidence_separates_registry_and_foursquare() -> None:
    evidence = age_evidence(
        [
            row("companies_house", created_date=date(2017, 1, 1)),
            row("foursquare", created_date=date(2019, 1, 1)),
            row("overture"),
        ]
    )
    assert evidence.registry == date(2017, 1, 1)
    assert evidence.fsq_date_created == date(2019, 1, 1)


def test_stored_age_survives_a_run_without_evidence() -> None:
    known = existing(created_date=date(2019, 1, 1), age_source="foursquare", age_unknown=False)
    result = combine_age(UNKNOWN, [known], NOW)
    assert result == AgeResult(date(2019, 1, 1), "foursquare", False)


def test_first_seen_is_not_evidence() -> None:
    seen = existing(created_date=date(2018, 5, 1), age_source="first_seen")
    result = combine_age(UNKNOWN, [seen], NOW)
    assert result == AgeResult(NOW.date(), "first_seen", True)


def test_survivor_is_the_contacted_lead() -> None:
    older = existing("a", first_seen=datetime(2025, 1, 1, tzinfo=UTC))
    contacted = existing("b", contacted=True)
    assert choose_survivor([older, contacted]).lead_id == "b"
    assert choose_survivor([existing("c"), older]).lead_id == "a"


def test_socials_from_rows_first_per_platform() -> None:
    rows = [
        row("foursquare", raw={"socials": ["https://www.facebook.com/1"]}),
        row("overture", raw={"socials": ["https://facebook.com/2", "https://twitter.com/x", "?"]}),
    ]
    assert socials_from_rows(rows) == {
        "facebook": "https://www.facebook.com/1",
        "x": "https://twitter.com/x",
    }


def test_build_lead_point() -> None:
    lead = build_lead("id", [row("overture", lat=35.9, lon=14.5)], age=UNKNOWN, seen_at=NOW)
    assert lead["geom"] == "SRID=4326;POINT(14.5 35.9)"
    assert "lat" not in lead
