"""Monthly collect + refresh flow (1st of month, 02:00 local)."""

from __future__ import annotations

from typing import Any

from prefect import flow, get_run_logger, task

from leads.db import finish_run, start_run
from leads.settings import get_settings, load_countries


@task(retries=2, retry_delay_seconds=600)
def collect_source(name: str, run_id: str) -> int:
    from leads.sources import collect

    settings = get_settings()
    counts = collect(
        [name], settings=settings, countries=load_countries(), since=settings.since, run_id=run_id
    )
    return counts[name]


@task
def process(run_id: str) -> dict[str, int]:
    from leads.pipeline import process_run

    return process_run(run_id)


@task(retries=2, retry_delay_seconds=300)
def screen_sanctions() -> dict[str, int]:
    from leads.pipeline.sanctions import screen_leads

    return screen_leads(refresh_list=True)


@task
def refresh(run_id: str) -> dict[str, int]:
    from leads.pipeline.refresh import run_refresh

    return run_refresh(run_id)


@task
def check_websites() -> dict[str, int]:
    from leads.enrich.check import check_pending

    return check_pending(recheck=True)


@flow(name="monthly-collect", log_prints=True)
def monthly_collect(sources: list[str] | None = None) -> dict[str, Any]:
    """Collect all enabled sources -> process -> sanctions -> refresh -> website checks."""
    logger = get_run_logger()
    settings = get_settings()
    names = sources or [n for n in settings.sources if settings.source_enabled(n)]
    run_id = start_run("monthly-collect")
    counts: dict[str, Any] = {"sources": {}}
    try:
        for name in names:
            counts["sources"][name] = collect_source(name, str(run_id))
            logger.info("collected %s rows from %s", counts["sources"][name], name)
        counts["process"] = process(str(run_id))
        counts["sanctions"] = screen_sanctions()
        counts["refresh"] = refresh(str(run_id))
        counts["websites"] = check_websites()
    except Exception:
        finish_run(run_id, "failed", counts)
        raise
    finish_run(run_id, "success", counts)
    return counts
