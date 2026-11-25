"""Gmail sync flow (every 30 minutes)."""

from __future__ import annotations

from prefect import flow

from leads.alerts import on_flow_failure
from leads.db import finish_run, start_run
from leads.settings import get_settings


@flow(
    name="gmail-sync",
    retries=1,
    retry_delay_seconds=300,
    log_prints=True,
    on_failure=[on_flow_failure],
    on_crashed=[on_flow_failure],
)
def gmail_sync() -> dict[str, int]:
    from leads.outreach.gmail_sync import sync

    if not get_settings().token_path.is_file():
        return {"not_connected": 1}
    run_id = start_run("gmail-sync")
    try:
        counts = sync()
    except Exception:
        finish_run(run_id, "failed")
        raise
    finish_run(run_id, "success", counts)
    return counts
