"""Tells the user when a scheduled job fails: a message in their own Gmail inbox.

The message is placed in the mailbox (messages.insert); nothing is sent to anyone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from leads.db import session_scope
from leads.models import Run
from leads.settings import get_settings

log = structlog.get_logger(__name__)


def failed_before(flow_name: str) -> bool:
    """Whether the run before the latest one failed too, so it was already reported."""
    with session_scope() as session:
        statuses = session.scalars(
            select(Run.status)
            .where(Run.flow == flow_name, Run.status != "running")
            .order_by(Run.started_at.desc())
            .limit(2)
        ).all()
    return len(statuses) == 2 and all(s == "failed" for s in statuses)


def place_in_inbox(subject: str, text: str, service: Any = None) -> None:
    from leads.outreach.drafts import check_account
    from leads.outreach.gmail_api import execute
    from leads.outreach.gmail_drafts import build_mime, encode_message

    settings = get_settings()
    if service is None:
        from leads.google_auth import gmail_service

        service = gmail_service(settings)
    check_account(service, settings)
    account = str(settings.gmail_account)
    message = build_mime(account, subject, text)
    message["From"] = f"Lead Automation <{account}>"
    body = {**encode_message(message), "labelIds": ["INBOX", "UNREAD", "IMPORTANT"]}
    execute(
        service.users().messages().insert(userId=settings.gmail_user_id, body=body),
        idempotent=False,
    )


def report_failure(flow_name: str, reason: str, service: Any = None) -> bool:
    """Report a failed job once until it works again. Never raises."""
    try:
        if failed_before(flow_name):
            return False
        place_in_inbox(
            f"Lead Automation: {flow_name} failed",
            f"The scheduled job '{flow_name}' failed on {datetime.now(UTC):%d %b %Y %H:%M} UTC.\n\n"
            f"Reason: {reason[:500]}\n\n"
            "See the Setup page on http://localhost:3300 and the job log on "
            "http://localhost:4200.\n",
            service,
        )
    except Exception as exc:
        log.warning("alert_not_delivered", flow=flow_name, error=repr(exc))
        return False
    return True


def on_flow_failure(flow: Any, flow_run: Any, state: Any) -> None:
    """Prefect hook, called after the last retry."""
    report_failure(str(flow.name), str(getattr(state, "message", "") or "unknown"))
