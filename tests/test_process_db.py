"""process_run against a real PostGIS database. Set TEST_DATABASE_URL to run (it is wiped)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

import leads.db as db
from leads.models import Base, DomainAge, Lead, LeadSource, LeadStatus, Outreach, Run
from leads.pipeline import process, refresh, wayback
from leads.settings import CollectSettings, Settings
from leads.sources.base import RawRecord, write_raw

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    assert URL and URL.rsplit("/", 1)[-1].endswith("_test"), "refusing to wipe a non-test database"
    settings = Settings(database_url=URL, data_dir=tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(process, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()
    with db.get_engine().begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
    yield tmp_path
    db.get_engine().dispose()
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()


def rec(source: str, rid: str, name: str, **kw: Any) -> RawRecord:
    return RawRecord(source=source, source_record_id=rid, name=name, **{"country_iso2": "MT", **kw})


def collect(data_dir: Path, *records: RawRecord) -> str:
    run_id = str(uuid.uuid4())
    for source in {r.source for r in records}:
        rows = [r for r in records if r.source == source]
        write_raw(rows, source=source, run_id=run_id, data_dir=data_dir)
    return run_id


def run(data_dir: Path, *records: RawRecord) -> dict[str, Any]:
    return process.process_run(collect(data_dir, *records))


def leads() -> list[Lead]:
    with db.session_scope() as session:
        return list(session.scalars(select(Lead).order_by(Lead.name)))


def count(model: Any) -> int:
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def add_outreach(lead_id: str) -> None:
    with db.session_scope() as session:
        session.add(Outreach(lead_id=lead_id, stage="first"))


def test_loads_and_is_idempotent(data_dir: Path) -> None:
    run_id = collect(
        data_dir,
        rec("overture", "1", "Blue Cafe", phone="+35621234567", lat=35.9, lon=14.5),
        rec("overture", "2", "Tokyo Ramen", country_iso2="JP"),
    )
    process.process_run(run_id)
    process.process_run(run_id)
    (lead,) = leads()
    assert (lead.name, lead.age_unknown, lead.status) == ("Blue Cafe", True, "active")
    assert lead.geom == "SRID=4326;POINT(14.5 35.9)"
    assert (count(LeadSource), count(Run)) == (1, 1)


def test_sources_collected_separately_share_one_lead(data_dir: Path) -> None:
    run(data_dir, rec("overture", "1", "Blue Cafe", shared_ids=["foursquare:f1"]))
    (before,) = leads()
    run(
        data_dir,
        rec("foursquare", "f1", "Blue Café", shared_ids=["foursquare:f1"],
            created_date=date(2020, 1, 1), email="Hi@Blue.example"),
    )  # fmt: skip
    (after,) = leads()
    assert after.lead_id == before.lead_id
    assert (after.created_date, after.age_unknown) == (date(2020, 1, 1), False)
    assert after.sources == ["foursquare", "overture"]
    assert after.email == "hi@blue.example"


def test_old_business_stays_out_when_sources_come_later(data_dir: Path) -> None:
    old = rec("foursquare", "f1", "Old Inn", shared_ids=["foursquare:f1"],
              created_date=date(2011, 1, 1))  # fmt: skip
    run(data_dir, old)
    assert leads() == []
    run(data_dir, rec("overture", "1", "Old Inn", shared_ids=["foursquare:f1"]))
    assert leads() == []


def test_lead_found_to_be_old_is_removed_or_closed(data_dir: Path) -> None:
    run(
        data_dir,
        rec("overture", "1", "Old Inn", shared_ids=["foursquare:f1"]),
        rec("overture", "2", "Zed Bar", shared_ids=["foursquare:f2"]),
    )
    add_outreach(next(x.lead_id for x in leads() if x.name == "Zed Bar"))
    run(
        data_dir,
        *(rec("foursquare", f, n, shared_ids=[f"foursquare:{f}"], created_date=date(2011, 1, 1))
          for f, n in (("f1", "Old Inn"), ("f2", "Zed Bar"))),
    )  # fmt: skip
    (lead,) = leads()
    assert (lead.name, lead.status) == ("Zed Bar", LeadStatus.CLOSED)
    assert count(Outreach) == 1
    run(data_dir, rec("overture", "2", "Zed Bar"))
    assert leads()[0].status == LeadStatus.CLOSED


def test_two_leads_that_turn_out_to_be_one_are_merged(data_dir: Path) -> None:
    run(data_dir, rec("overture", "1", "Blue Cafe"), rec("overture", "2", "Blue Cafe Valletta"))
    contacted = next(x for x in leads() if x.name == "Blue Cafe Valletta")
    add_outreach(contacted.lead_id)
    phone = "+35621234567"
    run(
        data_dir,
        rec("overture", "1", "Blue Cafe", phone=phone),
        rec("overture", "2", "Blue Cafe Valletta", phone=phone),
    )
    (lead,) = leads()
    assert lead.lead_id == contacted.lead_id
    assert (count(Outreach), count(LeadSource)) == (1, 2)


def test_kept_lead_is_stable_when_its_other_record_turns_out_old(data_dir: Path) -> None:
    run(
        data_dir,
        rec("overture", "1", "Old Inn", shared_ids=["foursquare:f1", "x:1"]),
        rec("overture", "2", "Zed Bar", shared_ids=["x:1"]),
    )
    (joined,) = leads()
    second = collect(
        data_dir,
        rec("overture", "1", "Old Inn", shared_ids=["foursquare:f1"]),
        rec("overture", "2", "Zed Bar"),
        rec("foursquare", "f1", "Old Inn", shared_ids=["foursquare:f1"],
            created_date=date(2011, 1, 1)),
    )  # fmt: skip
    for _ in range(2):
        counts = process.process_run(second)
        (lead,) = leads()
        assert (lead.name, lead.lead_id) == ("Zed Bar", joined.lead_id)
        assert (counts["updated"], counts.get("retired", 0)) == (1, 0)
    assert count(LeadSource) == 1


def test_lead_whose_records_split_keeps_its_id_once(data_dir: Path) -> None:
    run(
        data_dir,
        rec("overture", "1", "Blue Cafe", shared_ids=["x:1"]),
        rec("overture", "2", "Red Garage", shared_ids=["x:1"]),
    )
    (joined,) = leads()
    run(data_dir, rec("overture", "1", "Blue Cafe"), rec("overture", "2", "Red Garage"))
    ids = {x.lead_id for x in leads()}
    assert len(ids) == 2
    assert joined.lead_id in ids


def test_check_results_survive_until_the_website_changes(data_dir: Path) -> None:
    run(data_dir, rec("overture", "1", "Blue Cafe", website="https://blue.example"))
    with db.session_scope() as session:
        lead = session.scalars(select(Lead)).one()
        lead.website_status, lead.whatsapp_number = "broken", "+35699999999"
    run(data_dir, rec("overture", "1", "Blue Cafe"))
    (lead,) = leads()
    assert (lead.website_status, lead.website) == ("broken", "https://blue.example")
    run(data_dir, rec("overture", "1", "Blue Cafe", website="https://new.example"))
    (lead,) = leads()
    assert (lead.website_status, lead.whatsapp_number) == (None, "+35699999999")


def test_non_businesses_and_platform_pages(data_dir: Path) -> None:
    fort = ["cultural_and_historic", "historic_site", "fort"]
    counts = run(
        data_dir,
        rec("overture", "1", "Blue Cafe", website="https://linktr.ee/blue"),
        rec("overture", "2", "Old Fort", category="fort", raw={"category_path": fort}),
    )
    (lead,) = leads()
    assert (lead.name, lead.website, lead.domain) == ("Blue Cafe", None, None)
    assert lead.socials == {"listing": "https://linktr.ee/blue"}
    assert counts["not_business"] == 1


def test_registry_dates_a_place_and_removes_an_old_one(data_dir: Path) -> None:
    place = {"country_iso2": "GB", "postcode": "LS1 4AP"}
    counts = run(
        data_dir,
        rec("overture", "1", "Blue Cafe", **place),
        rec("overture", "2", "Old Inn", **place),
        rec("companies_house", "01", "BLUE CAFE LTD", created_date=date(2019, 1, 31), **place),
        rec("companies_house", "02", "OLD INN LIMITED", created_date=date(1990, 1, 1), **place),
        rec("companies_house", "03", "UNRELATED LTD", created_date=date(2020, 1, 1), **place),
    )
    (lead,) = leads()
    assert (lead.name, lead.age_source) == ("Blue Cafe", "registry")
    assert (lead.created_date, lead.age_unknown) == (date(2019, 1, 31), False)
    assert counts["too_old"] == 1


def test_registry_collected_after_the_places(data_dir: Path) -> None:
    place = {"country_iso2": "GB", "postcode": "LS1 4AP"}
    run(data_dir, rec("overture", "1", "Blue Cafe", **place))
    run(data_dir, rec("companies_house", "01", "Blue Cafe Ltd", created_date=date(2019, 1, 31),
                      **place))  # fmt: skip
    (lead,) = leads()
    assert (lead.age_source, lead.sources) == ("registry", ["companies_house", "overture"])


def test_contactable_new_company_is_a_lead_of_its_own(data_dir: Path) -> None:
    run(
        data_dir,
        rec("brreg", "1", "Ny AS", country_iso2="NO", email="post@ny.no",
            category="56.110 Restauranter", created_date=date(2024, 1, 1)),
        rec("brreg", "2", "Gammel AS", country_iso2="NO", email="post@gammel.no",
            created_date=date(1990, 1, 1)),
        rec("brreg", "3", "Stille AS", country_iso2="NO", created_date=date(2024, 1, 1)),
    )  # fmt: skip
    (lead,) = leads()
    assert (lead.name, lead.email) == ("Ny AS", "post@ny.no")
    assert (lead.category_group, lead.age_source) == ("food_drink", "registry")


@pytest.fixture
def archive(monkeypatch: pytest.MonkeyPatch) -> dict[str, date | None]:
    captures: dict[str, date | None] = {}

    def lookup(domain: str, **_: Any) -> date | None:
        if domain not in captures:
            raise httpx.ConnectError("down")
        return captures[domain]

    monkeypatch.setattr(wayback, "wayback_first_capture", lookup)
    monkeypatch.setattr(wayback.time, "sleep", lambda _: None)
    monkeypatch.setattr(wayback, "get_settings", db.get_settings)
    return captures


def test_wayback_dates_removes_and_remembers(
    data_dir: Path, archive: dict[str, date | None]
) -> None:
    records = [
        rec("overture", str(i), name, website=f"https://{name}.com")
        for i, name in enumerate(("fresh", "ancient", "unseen"))
    ]
    run(data_dir, *records)
    archive |= {"fresh.com": date(2021, 1, 1), "ancient.com": date(2005, 1, 1)}
    archive["unseen.com"] = None
    counts = wayback.check_wayback()
    assert counts == {"checked": 3, "dated": 1, "too_old": 1, "not_archived": 1, "failed": 0}
    assert [(x.name, x.age_source) for x in leads()] == [
        ("fresh", "wayback"),
        ("unseen", "first_seen"),
    ]
    assert wayback.check_wayback()["checked"] == 0
    run(data_dir, *records)
    assert [x.name for x in leads()] == ["fresh", "unseen"]


def test_wayback_closes_a_contacted_lead(data_dir: Path, archive: dict[str, date | None]) -> None:
    run(data_dir, rec("overture", "1", "Old Inn", website="https://inn.com"))
    add_outreach(leads()[0].lead_id)
    archive["inn.com"] = date(2005, 1, 1)
    wayback.check_wayback()
    assert leads()[0].status == LeadStatus.CLOSED
    assert count(Outreach) == 1


def test_wayback_stops_when_the_archive_is_down(
    data_dir: Path, archive: dict[str, date | None]
) -> None:
    run(data_dir, *(rec("overture", str(i), f"n{i}", website=f"https://n{i}.com")
                    for i in range(9)))  # fmt: skip
    assert wayback.check_wayback()["failed"] == wayback.MAX_FAILURES_IN_A_ROW
    assert count(DomainAge) == 0


def test_unknown_run(data_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        process.process_run(uuid.uuid4())


def _monthly(
    days_ago: int, areas: dict[str, Any], sources: list[str], status: str = "success"
) -> Run:
    return Run(
        run_id=uuid.uuid4(),
        flow="monthly-collect",
        started_at=datetime.now(UTC) - timedelta(days=days_ago),
        status=status,
        counts={"coverage": {"sources": sources, "areas": areas}},
    )


def _seen(lead_id: str, days_ago: int, source: str, lon: float = 14.5) -> list[Any]:
    return [
        Lead(lead_id=lead_id, name=lead_id, country_iso2="MT", geom=f"SRID=4326;POINT({lon} 35.9)",
             last_seen=datetime.now(UTC) - timedelta(days=days_ago)),
        LeadSource(lead_id=lead_id, source=source, source_record_id=lead_id),
    ]  # fmt: skip


BOX = {"MT": [14.1, 35.7, 14.7, 36.2]}


@pytest.fixture
def refresh_db(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(data_dir=data_dir)
    settings.refresh.max_closed_share = 0.5
    monkeypatch.setattr(refresh, "get_settings", lambda: settings)
    with db.session_scope() as s:
        for row in (
            *_seen("fresh", 0, "overture"),
            *_seen("fresh2", 0, "overture"),
            *_seen("gone", 60, "overture"),
            *_seen("other_source", 60, "foursquare"),
            *_seen("outside", 60, "overture", lon=20.0),
        ):
            s.add(row)
            s.flush()
    return settings


def _statuses() -> dict[str, str]:
    with db.session_scope() as s:
        return {r.lead_id: r.status for r in s.scalars(select(Lead))}


def test_refresh_closes_only_what_complete_runs_missed(refresh_db: Settings) -> None:
    with db.session_scope() as s:
        s.add(_monthly(30, BOX, ["overture"]))
    assert refresh.run_refresh()["closed"] == 0  # one run is not enough
    with db.session_scope() as s:
        s.add(_monthly(1, BOX, ["overture"], status="failed"))
    assert refresh.run_refresh()["closed"] == 0
    with db.session_scope() as s:
        s.add(_monthly(1, BOX, ["overture"]))
    assert refresh.run_refresh() == {"covered": 3, "missing": 1, "closed": 1}
    assert {k for k, v in _statuses().items() if v == "closed"} == {"gone"}


def test_refresh_stops_when_too_many_are_missing(refresh_db: Settings) -> None:
    refresh_db.refresh.max_closed_share = 0.1
    with db.session_scope() as s:
        s.add_all([_monthly(30, BOX, ["overture"]), _monthly(1, BOX, ["overture"])])
    counts = refresh.run_refresh()
    assert counts["closed"] == 0 and counts["skipped_too_many"] == 1
    assert "closed" not in _statuses().values()


def test_coverage_ignores_empty_sources() -> None:
    settings = Settings(country_bbox={"MT": (1, 2, 3, 4)})
    found = refresh.coverage_counts({"overture": 5, "foursquare": 0}, ["MT"], settings)
    assert found == {"sources": ["overture"], "areas": {"MT": [1, 2, 3, 4]}}


def test_collect_countries() -> None:
    included = frozenset({"GB", "IE"})
    assert CollectSettings().codes(included) == included
    assert CollectSettings(countries=["gb", "XX"]).codes(included) == {"GB"}
    assert CollectSettings(countries=["XX"]).codes(included) == frozenset()
