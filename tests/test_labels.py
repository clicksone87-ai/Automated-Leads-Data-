from __future__ import annotations

from typing import Any

from leads.models import Stage
from leads.outreach.labels import (
    ensure_labels,
    label_with_parents,
    labels_for_lead,
    other_stage_labels,
    stage_label,
)


def test_labels_for_lead() -> None:
    assert labels_for_lead(
        country_iso2="gb",
        category_group="Food & Drink",
        website_status="broken",
        stage=Stage.FIRST,
    ) == [
        "Leads/Country/GB",
        "Leads/Category/Food & Drink",
        "Leads/Website/broken",
        "Leads/Stage/First email",
    ]


def test_stage_names() -> None:
    assert stage_label("followup") == "Leads/Stage/Follow-up"
    assert stage_label(Stage.REPLIED) == "Leads/Stage/Replied"
    assert stage_label(Stage.OPTED_OUT) == "Leads/Stage/Opted out"


def test_slash_and_missing_values_cannot_break_nesting() -> None:
    labels = labels_for_lead(
        country_iso2=None, category_group="Cafés/Bars", website_status=None, stage=Stage.FIRST
    )
    assert labels[0] == "Leads/Country/Unknown"
    assert labels[1] == "Leads/Category/Cafés-Bars"
    assert labels[2] == "Leads/Website/Unknown"


def test_other_stage_labels() -> None:
    others = other_stage_labels(Stage.REPLIED)
    assert "Leads/Stage/Replied" not in others
    assert len(others) == 3


def test_label_with_parents() -> None:
    assert label_with_parents("Leads/Country/GB") == ["Leads", "Leads/Country", "Leads/Country/GB"]


class FakeGmail:
    """Just enough of the Gmail API resource for ensure_labels."""

    def __init__(self, existing: dict[str, str]) -> None:
        self.labels_store = dict(existing)
        self.created: list[str] = []

    def users(self) -> FakeGmail:
        return self

    def labels(self) -> FakeGmail:
        return self

    def list(self, userId: str) -> Any:
        store = self.labels_store
        return _Exec({"labels": [{"name": n, "id": i} for n, i in store.items()]})

    def create(self, userId: str, body: dict[str, Any]) -> Any:
        new_id = f"Label_{len(self.labels_store)}"
        self.labels_store[body["name"]] = new_id
        self.created.append(body["name"])
        return _Exec({"id": new_id, "name": body["name"]})


class _Exec:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def execute(self) -> dict[str, Any]:
        return self.result


def test_ensure_labels_creates_missing_parents_first() -> None:
    gmail = FakeGmail({"Leads": "Label_A"})
    ids = ensure_labels(gmail, ["Leads/Country/GB", "Leads/Country/FR"])
    assert gmail.created == ["Leads/Country", "Leads/Country/GB", "Leads/Country/FR"]
    assert set(ids) == {"Leads/Country/GB", "Leads/Country/FR"}
