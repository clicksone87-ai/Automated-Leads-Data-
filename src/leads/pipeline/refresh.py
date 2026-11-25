"""Monthly refresh: close businesses that several complete collect runs no longer list."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import Float, and_, cast, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from leads.db import session_scope
from leads.models import Lead, LeadSource, LeadStatus, Run
from leads.settings import Settings, get_settings

log = structlog.get_logger(__name__)

FLOW = "monthly-collect"
# Sources that list open businesses in full. Registries are matched to leads, not listed.
PLACE_SOURCES = frozenset({"overture", "foursquare"})


@dataclass(frozen=True, slots=True)
class Coverage:
    """What one collect run looked at."""

    started_at: datetime
    sources: frozenset[str]
    areas: Mapping[str, tuple[float, float, float, float] | None]  # country -> box


def coverage_counts(
    sources: Mapping[str, int], countries: Sequence[str], settings: Settings
) -> dict[str, Any]:
    """Coverage as stored in ``runs.counts``. A source that returned nothing covers nothing."""
    boxed = all(c in settings.country_bbox for c in countries)
    return {
        "sources": sorted(name for name, rows in sources.items() if rows),
        "areas": {c: list(settings.country_bbox[c]) if boxed else None for c in sorted(countries)},
    }


def _coverage(run: Run) -> Coverage | None:
    stored = run.counts.get("coverage")
    if not stored:
        return None
    return Coverage(
        started_at=run.started_at,
        sources=frozenset(stored["sources"]) & PLACE_SOURCES,
        areas={c: tuple(b) if b else None for c, b in stored["areas"].items()},
    )


def covered_by(runs: Sequence[Coverage]) -> ColumnElement[bool] | None:
    """Leads that each of the runs would have listed if they still existed.

    That is, all their place sources were read and they lie in an area all runs covered; a
    partial run says nothing about the rest.
    """
    sources = frozenset.intersection(*(r.sources for r in runs)) if runs else frozenset()
    if not sources:
        return None
    countries = set.intersection(*(set(r.areas) for r in runs))
    places: list[ColumnElement[bool]] = []
    for country in sorted(countries):
        parts: list[ColumnElement[bool]] = [Lead.country_iso2 == country]
        boxes = [b for r in runs if (b := r.areas[country])]
        for west, south, east, north in boxes:
            parts.append(
                func.ST_Within(
                    Lead.__table__.c.geom,
                    func.ST_MakeEnvelope(
                        cast(west, Float), cast(south, Float), cast(east, Float),
                        cast(north, Float), 4326,
                    ),
                )
            )  # fmt: skip
        places.append(and_(*parts))
    if not places:
        return None
    own = select(LeadSource.source).where(
        LeadSource.lead_id == Lead.lead_id, LeadSource.source.in_(PLACE_SOURCES)
    )
    unread = own.where(LeadSource.source.not_in(sources))
    return and_(Lead.status == LeadStatus.ACTIVE, own.exists(), ~unread.exists(), or_(*places))


def complete_runs(session: Session, current: uuid.UUID | None, wanted: int) -> list[Coverage]:
    """The newest ``wanted`` runs that can be judged, the current one included."""
    rows = session.scalars(
        select(Run)
        .where(Run.flow == FLOW, or_(Run.status == "success", Run.run_id == current))
        .order_by(Run.started_at.desc())
        .limit(wanted)
    )
    return [c for run in rows if (c := _coverage(run))]


def run_refresh(run_id: uuid.UUID | str | None = None) -> dict[str, int]:
    """Close leads missing from the last few collect runs. A new sighting reopens them."""
    settings = get_settings()
    current = uuid.UUID(run_id) if isinstance(run_id, str) else run_id
    wanted = settings.refresh.closed_after_missed_runs
    counts = {"covered": 0, "missing": 0, "closed": 0}
    with session_scope() as session:
        runs = complete_runs(session, current, wanted)
        if len(runs) < wanted:
            return counts
        covered = covered_by(runs)
        if covered is None:
            return counts
        oldest = min(r.started_at for r in runs)
        unseen = and_(covered, Lead.last_seen < oldest)
        counts["covered"] = session.scalar(select(func.count()).where(covered)) or 0
        counts["missing"] = session.scalar(select(func.count()).where(unseen)) or 0
        if not counts["missing"]:
            return counts
        if counts["missing"] > counts["covered"] * settings.refresh.max_closed_share:
            log.warning("refresh_skipped", reason="too many leads missing", **counts)
            counts["skipped_too_many"] = 1
            return counts
        session.execute(
            update(Lead)
            .where(unseen)
            .values(status=LeadStatus.CLOSED.value, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        counts["closed"] = counts["missing"]
    return counts
