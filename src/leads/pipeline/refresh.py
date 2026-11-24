"""Monthly refresh: re-match new releases, update last_seen, close missing leads,
re-check stale websites."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta


def should_close(missed_runs: int, closed_after_missed_runs: int = 2) -> bool:
    """A lead missing from this many consecutive collect runs is marked closed."""
    return missed_runs >= closed_after_missed_runs


def needs_website_recheck(
    checked_at: datetime | None, now: datetime, recheck_after_days: int = 30
) -> bool:
    return checked_at is None or now - checked_at >= timedelta(days=recheck_after_days)


def run_refresh(run_id: uuid.UUID | str | None = None) -> dict[str, int]:
    """Monthly refresh after a collect run has been processed.

    TODO(build step 6):
      1. ``last_seen = run.started_at`` for every lead with a lead_sources row in this run
         (done during upsert in ``pipeline.process_run``).
      2. Find the last ``closed_after_missed_runs`` successful 'monthly-collect' runs; leads
         whose ``last_seen`` is older than the earliest of them -> ``status = 'closed'``
         (``should_close``). Never reopen automatically without a new sighting.
      3. Leads with ``closed_date`` from Foursquare / dissolved in a registry -> closed.
      4. Queue website re-checks where ``needs_website_recheck`` (> 30 days).
      5. Return counts: {"seen": n, "closed": n, "recheck_queued": n}.
    """
    raise NotImplementedError("refresh.run_refresh (build step 6)")
