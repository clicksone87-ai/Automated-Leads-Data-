"""process_run against a real PostGIS database. Set TEST_DATABASE_URL to run (it is wiped)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text

import leads.db as db
from leads.models import Base, Lead, LeadSource, LeadStatus, Outreach, Run
from leads.pipeline import process
from leads.settings import Settings
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
    run(data_dir, rec("overture", "1", "Blue Cafe"), rec("overture", "2", "Blue Restaurant"))
    contacted = next(x for x in leads() if x.name == "Blue Restaurant")
    add_outreach(contacted.lead_id)
    phone = "+35621234567"
    run(
        data_dir,
        rec("overture", "1", "Blue Cafe", phone=phone),
        rec("overture", "2", "Blue Restaurant", phone=phone),
    )
    (lead,) = leads()
    assert lead.lead_id == contacted.lead_id
    assert (count(Outreach), count(LeadSource)) == (1, 2)


def test_lead_whose_records_split_keeps_its_id_once(data_dir: Path) -> None:
    phone = "+35621234567"
    run(
        data_dir,
        rec("overture", "1", "Blue Cafe", phone=phone),
        rec("overture", "2", "Red Garage", phone=phone),
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


def test_unknown_run(data_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        process.process_run(uuid.uuid4())
