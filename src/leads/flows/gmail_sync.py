"""Gmail sync flow (every 30 minutes)."""

from __future__ import annotations

from prefect import flow


@flow(name="gmail-sync", retries=1, retry_delay_seconds=300, log_prints=True)
def gmail_sync() -> dict[str, int]:
    from leads.outreach.gmail_sync import sync

    return sync()
