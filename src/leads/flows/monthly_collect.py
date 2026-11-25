"""Monthly collect + refresh flow (1st of month, 02:00 local)."""

from __future__ import annotations

import shutil
from typing import Any

from prefect import flow, get_run_logger, task

from leads.alerts import on_flow_failure
from leads.db import finish_run, save_counts, start_run
from leads.pipeline.refresh import coverage_counts
from leads.settings import get_settings, load_countries
from leads.sources.base import raw_dir


@task(retries=2, retry_delay_seconds=600)
def collect_source(name: str, run_id: str) -> int:
    from leads.sources import collect

    settings = get_settings()
    # A retry must not leave parts of the failed try behind.
    shutil.rmtree(raw_dir(settings.data_dir, name, run_id), ignore_errors=True)
    counts = collect(
        [name],
        settings=settings,
        countries=settings.collect.codes(load_countries()),
        since=settings.since,
        run_id=run_id,
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


@flow(
    name="monthly-collect",
    log_prints=True,
    on_failure=[on_flow_failure],
    on_crashed=[on_flow_failure],
)
def monthly_collect(sources: list[str] | None = None) -> dict[str, Any]:
    """Collect all enabled sources -> process -> sanctions -> refresh.

    Websites are (re)checked by the daily run, a few thousand a day.
    """
    logger = get_run_logger()
    settings = get_settings()
    names = sources or [n for n in settings.sources if settings.source_enabled(n)]
    if not settings.collect.codes(load_countries()):
        raise ValueError("collect.countries names no country from countries.yaml")
    run_id = start_run("monthly-collect")
    counts: dict[str, Any] = {"sources": {}}
    try:
        for name in names:
            counts["sources"][name] = collect_source(name, str(run_id))
            logger.info("collected %s rows from %s", counts["sources"][name], name)
        countries = sorted(settings.collect.codes(load_countries()))
        counts["coverage"] = coverage_counts(counts["sources"], countries, settings)
        save_counts(run_id, counts)
        counts["process"] = process(str(run_id))
        counts["sanctions"] = screen_sanctions()
        counts["refresh"] = refresh(str(run_id))
    except Exception:
        finish_run(run_id, "failed", counts)
        raise
    finish_run(run_id, "success", counts)
    return counts
