from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from leads import deploy
from leads.settings import CollectSettings, Settings
from leads.sources.registries import REGISTRY_SOURCES


class FakeSession:
    def __init__(self, found: Any) -> None:
        self.found = found

    def scalar(self, _query: Any) -> Any:
        return self.found


def use_session(monkeypatch: pytest.MonkeyPatch, found: Any) -> None:
    @contextmanager
    def scope() -> Any:
        yield FakeSession(found)

    monkeypatch.setattr(deploy, "session_scope", scope)


def test_collect_is_due_without_a_recent_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    use_session(monkeypatch, None)
    assert deploy.collect_due() is True


def test_collect_is_not_due_after_a_recent_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_session(monkeypatch, object())
    assert deploy.collect_due() is False


def test_collect_on_start_is_on_by_default() -> None:
    # The class default, not Settings(): a laptop's .env may switch it off.
    assert CollectSettings().on_start is True


def test_start_collect_retries_until_the_deployment_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def run_deployment(name: str, timeout: int) -> None:
        calls.append(name)
        if len(calls) < 3:
            raise LookupError("deployment not registered yet")

    import prefect.deployments

    monkeypatch.setattr(prefect.deployments, "run_deployment", run_deployment)
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    deploy.start_collect_when_served()
    assert calls == [deploy.COLLECT_DEPLOYMENT] * 3


def test_every_registry_is_switched_on() -> None:
    settings = Settings()
    for name in REGISTRY_SOURCES:
        assert settings.source_enabled(name), name


def test_gmail_sign_in_from_env_values(tmp_path: Any) -> None:
    from pydantic import SecretStr

    no_file = tmp_path / "token.json"
    empty = Settings(
        google_token_file=no_file,
        google_client_id="",
        google_client_secret=None,
        google_refresh_token=None,
    )
    assert not empty.google_signed_in
    given = Settings(
        google_token_file=no_file,
        google_client_id="id.apps.googleusercontent.com",
        google_client_secret=SecretStr("secret"),
        google_refresh_token=SecretStr("1//refresh"),
    )
    assert given.google_env_sign_in and given.google_signed_in


def test_print_env_lines_come_from_the_token(tmp_path: Any) -> None:
    import json

    from leads.google_auth import env_lines

    token = tmp_path / "token.json"
    token.write_text(json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))
    assert env_lines(token) == [
        "GOOGLE_CLIENT_ID=a",
        "GOOGLE_CLIENT_SECRET=b",
        "GOOGLE_REFRESH_TOKEN=c",
    ]
