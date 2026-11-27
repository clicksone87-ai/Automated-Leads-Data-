"""Register Prefect deployments with their schedules, then serve them (blocks forever).

Runs as the ``worker`` container's command: ``python -m leads.deploy``.
Schedules use ``settings.timezone``; cron strings come from config/settings.yaml.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from prefect import serve
from prefect.schedules import Cron
from sqlalchemy import select

from leads import monitoring
from leads.db import session_scope
from leads.flows.backup import backup_check
from leads.flows.daily_drafts import daily_drafts
from leads.flows.gmail_sync import gmail_sync
from leads.flows.monthly_collect import monthly_collect
from leads.log import configure_logging
from leads.models import Run
from leads.settings import Settings, config_dir, get_settings

log = structlog.get_logger(__name__)

COLLECT_DEPLOYMENT = "monthly-collect/monthly-collect"
# A month plus a few days, so a run that finished late on the 1st still counts.
COLLECT_FRESH_DAYS = 35


def build_deployments(settings: Settings) -> list[Any]:
    tz = settings.timezone
    sched = settings.schedules
    return [
        monthly_collect.to_deployment(
            name="monthly-collect",
            schedule=Cron(sched.monthly_collect, timezone=tz),
            description="Collect sources, process, sanctions, refresh, re-check websites",
        ),
        daily_drafts.to_deployment(
            name="daily-drafts",
            schedule=Cron(sched.daily_drafts, timezone=tz),
            description="Website checks for new leads, Gmail drafts, Google Sheet update",
        ),
        gmail_sync.to_deployment(
            name="gmail-sync",
            schedule=Cron(sched.gmail_sync, timezone=tz),
            description="Sent / replied / bounced / opt-out detection and follow-up drafts",
        ),
        backup_check.to_deployment(
            name="backup-check",
            schedule=Cron(sched.backup_check, timezone=tz),
            description="Verify the nightly pg_dump from the backup container",
        ),
    ]


def migrate() -> None:
    """Bring the schema up to date, so a new server needs no manual migration step."""
    # A separate process: alembic's env.py reconfigures logging, which would silence ours.
    ini = config_dir().parent / "alembic.ini"
    subprocess.run([sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"], check=True)


def collect_due(now: datetime | None = None) -> bool:
    """True when no full collect finished successfully in the last ``COLLECT_FRESH_DAYS``."""
    since = (now or datetime.now(UTC)) - timedelta(days=COLLECT_FRESH_DAYS)
    with session_scope() as session:
        last = session.scalar(
            select(Run.started_at)
            .where(Run.flow == "monthly-collect", Run.status == "success", Run.started_at >= since)
            .limit(1)
        )
    return last is None


def start_collect_when_served(wait_seconds: float = 600) -> None:
    """Trigger the collect deployment once ``serve`` has registered it."""
    from prefect.deployments import run_deployment

    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            run_deployment(name=COLLECT_DEPLOYMENT, timeout=0)
        except Exception as exc:
            # serve() registers the deployments a moment after it starts.
            if time.monotonic() > deadline:
                log.error("start_collect_failed", error=repr(exc))
                return
            time.sleep(10)
            continue
        log.info("start_collect_triggered")
        return


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    monitoring.start(settings, "worker")
    migrate()
    if settings.collect.on_start and collect_due():
        threading.Thread(target=start_collect_when_served, daemon=True).start()
    serve(*build_deployments(settings))


if __name__ == "__main__":
    main()
