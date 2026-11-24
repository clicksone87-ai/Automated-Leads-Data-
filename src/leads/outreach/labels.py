"""Nested Gmail labels per segment.

Leads/Country/<ISO2>
Leads/Category/<group display name>
Leads/Website/<website_status>
Leads/Stage/<First email | Follow-up | Replied | Opted out>
"""

from __future__ import annotations

import re
from typing import Any

from leads.models import Stage

ROOT = "Leads"
STAGE_NAMES: dict[Stage, str] = {
    Stage.FIRST: "First email",
    Stage.FOLLOWUP: "Follow-up",
    Stage.REPLIED: "Replied",
    Stage.OPTED_OUT: "Opted out",
}


def _part(value: str | None) -> str:
    """One label path segment: no '/', collapsed whitespace, never empty."""
    cleaned = re.sub(r"\s+", " ", (value or "").replace("/", "-")).strip()
    return cleaned or "Unknown"


def country_label(country_iso2: str | None) -> str:
    return f"{ROOT}/Country/{_part(country_iso2.upper() if country_iso2 else None)}"


def category_label(category_group: str | None) -> str:
    return f"{ROOT}/Category/{_part(category_group)}"


def website_label(website_status: str | None) -> str:
    return f"{ROOT}/Website/{_part(website_status)}"


def stage_label(stage: Stage | str) -> str:
    return f"{ROOT}/Stage/{STAGE_NAMES[Stage(stage)]}"


def labels_for_lead(
    *,
    country_iso2: str | None,
    category_group: str | None,
    website_status: str | None,
    stage: Stage | str,
) -> list[str]:
    return [
        country_label(country_iso2),
        category_label(category_group),
        website_label(website_status),
        stage_label(stage),
    ]


def other_stage_labels(stage: Stage | str) -> list[str]:
    """Stage labels to remove from a thread when it moves to ``stage``."""
    current = Stage(stage)
    return [stage_label(s) for s in Stage if s != current]


def label_with_parents(name: str) -> list[str]:
    """``Leads/Country/GB`` -> ``["Leads", "Leads/Country", "Leads/Country/GB"]``."""
    parts = name.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def ensure_labels(service: Any, names: list[str], user_id: str = "me") -> dict[str, str]:
    """Create any missing labels (parents first) and return ``{name: label_id}``.

    ``service`` is a Gmail API resource from ``leads.google_auth.gmail_service()``.
    """
    existing = {
        lbl["name"]: lbl["id"]
        for lbl in service.users().labels().list(userId=user_id).execute().get("labels", [])
    }
    wanted: list[str] = []
    for name in names:
        for candidate in label_with_parents(name):
            if candidate not in wanted:
                wanted.append(candidate)
    for name in wanted:
        if name not in existing:
            body = {
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created = service.users().labels().create(userId=user_id, body=body).execute()
            existing[name] = created["id"]
    return {name: existing[name] for name in names}
