"""CSV export of eligible leads with the data attribution footer."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import false, select

from leads.db import session_scope
from leads.export import ATTRIBUTION_LINES, EXPORT_COLUMNS
from leads.models import Lead, LeadStatus
from leads.settings import get_settings


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return "" if value is None else value


def export_leads(path: Path | None = None, *, include_unknown_age: bool | None = None) -> Path:
    """Write active leads (sorted by country, category, name) to CSV. Returns the path."""
    settings = get_settings()
    include_unknown = (
        settings.include_unknown_age if include_unknown_age is None else include_unknown_age
    )
    if path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
        path = settings.data_dir / "exports" / f"leads_{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    columns = [getattr(Lead, c) for c in EXPORT_COLUMNS]
    stmt = select(*columns).where(
        Lead.status == LeadStatus.ACTIVE,
        Lead.sanctions_hit.is_(false()),
        Lead.created_date.is_(None) | (Lead.created_date >= settings.since) | Lead.age_unknown,
    )
    if not include_unknown:
        stmt = stmt.where(Lead.age_unknown.is_(false()))
    stmt = stmt.order_by(Lead.country_iso2, Lead.category_group, Lead.name)

    with session_scope() as session, path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPORT_COLUMNS)
        for row in session.execute(stmt).yield_per(5000):
            writer.writerow([_cell(v) for v in row])
        writer.writerow([])
        for line in ATTRIBUTION_LINES:
            writer.writerow([line])
    return path
