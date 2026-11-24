"""Register Prefect deployments with their schedules, then serve them (blocks forever).

Runs as the ``worker`` container's command: ``python -m leads.deploy``.
Schedules use ``settings.timezone``; cron strings come from config/settings.yaml.
"""

from __future__ import annotations

from typing import Any

from prefect import serve
from prefect.schedules import Cron

from leads.flows.backup import backup_check
from leads.flows.daily_drafts import daily_drafts
from leads.flows.gmail_sync import gmail_sync
from leads.flows.monthly_collect import monthly_collect
from leads.log import configure_logging
from leads.settings import Settings, get_settings


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


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    serve(*build_deployments(settings))


if __name__ == "__main__":
    main()
