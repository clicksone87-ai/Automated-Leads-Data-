"""Daily flow (07:00 local): opening dates and, if enabled, Gmail drafts.

Websites are checked all day by the checker service.
"""

from __future__ import annotations

from typing import Any

from prefect import flow, task

from leads.db import finish_run, start_run
from leads.settings import get_settings


@task
def date_leads() -> dict[str, int]:
    from leads.pipeline.wayback import check_wayback

    return check_wayback()


@task(retries=2, retry_delay_seconds=120)
def create_drafts(batch_size: int | None) -> dict[str, int]:
    from leads.outreach.gmail_drafts import create_daily_drafts

    return create_daily_drafts(batch_size)


@task(retries=2, retry_delay_seconds=120)
def update_sheet() -> dict[str, int]:
    from leads.export.sheets import update_sheet as _update_sheet

    if not get_settings().google_sheet_id:
        return {"rows": 0}

    return _update_sheet()


@flow(name="daily-drafts", log_prints=True)
def daily_drafts(batch_size: int | None = None) -> dict[str, Any]:
    run_id = start_run("daily-drafts")
    counts: dict[str, Any] = {}
    try:
        counts["wayback"] = date_leads()
        if get_settings().outreach.auto_drafts:
            counts["drafts"] = create_drafts(batch_size)
        counts["sheet"] = update_sheet()
    except Exception:
        finish_run(run_id, "failed", counts)
        raise
    finish_run(run_id, "success", counts)
    return counts
