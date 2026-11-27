from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from leads import deploy
from leads.settings import CollectSettings, Settings


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
    for name in ("companies_house", "sirene", "brreg", "abn", "canada"):
        assert settings.source_enabled(name), name
