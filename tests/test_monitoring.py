from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from leads import monitoring
from leads.settings import Settings


def test_off_without_a_dsn() -> None:
    assert monitoring.start(Settings(sentry_dsn=None), "test") is False
    monitoring.report("nothing happens")  # must not raise while off


def test_events_lose_variables_and_request_data() -> None:
    frame = {"function": "draft", "vars": {"email": "owner@example.com", "key": "secret"}}
    event: dict[str, Any] = {
        "user": {"email": "x@example.com"},
        "request": {"url": "/api/leads/1", "data": {"body": "hi"}, "headers": {"Cookie": "c"}},
        "exception": {"values": [{"stacktrace": {"frames": [frame]}}]},
        "breadcrumbs": {"values": [{"message": "Acme Ltd"}]},
    }
    cleaned = monitoring.scrub(event)
    assert "user" not in cleaned and "breadcrumbs" not in cleaned
    assert cleaned["request"] == {"url": "/api/leads/1"}
    assert "vars" not in cleaned["exception"]["values"][0]["stacktrace"]["frames"][0]


def test_dsn_is_a_secret() -> None:
    settings = Settings(sentry_dsn=SecretStr("https://key@o1.ingest.sentry.io/1"))
    assert "key@" not in repr(settings.sentry_dsn)
