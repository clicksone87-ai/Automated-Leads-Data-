"""Error reports to Sentry, switched on only when ``SENTRY_DSN`` is set.

Reports carry the error and its stack trace, never local variables, request bodies,
headers or cookies, so lead contacts and keys stay on this machine.
"""

from __future__ import annotations

from typing import Any, cast

from leads.settings import Settings

_started = False


def scrub(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drop everything that could hold lead data or secrets from an event."""
    event.pop("user", None)
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "headers", "query_string", "env"):
            request.pop(key, None)
    for exception in (event.get("exception") or {}).get("values") or []:
        for frame in (exception.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    for thread in (event.get("threads") or {}).get("values") or []:
        for frame in (thread.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    event.pop("breadcrumbs", None)  # may quote log lines with business names
    return event


def start(settings: Settings, component: str) -> bool:
    """Start Sentry once per process; returns whether it is on."""
    global _started
    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else ""
    if _started or not dsn:
        return _started
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment,
        server_name=component,
        traces_sample_rate=0.0,
        before_send=cast(Any, scrub),
    )
    _started = True
    return True


def report(error: BaseException | str) -> None:
    """Send a handled error or message, when Sentry is on."""
    if not _started:
        return
    import sentry_sdk

    if isinstance(error, BaseException):
        sentry_sdk.capture_exception(error)
    else:
        sentry_sdk.capture_message(error, level="error")
