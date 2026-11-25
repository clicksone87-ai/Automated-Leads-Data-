"""FastAPI app for the local dashboard. Serve it on 127.0.0.1 only: it has no login."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

import phonenumbers
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from leads.db import session_scope
from leads.export import ATTRIBUTION_LINES, EXPORT_COLUMNS
from leads.models import Lead, Suppression
from leads.outreach import drafts
from leads.outreach.ai import AiError
from leads.outreach.phrases import LANGUAGES, lead_language
from leads.settings import Settings, get_settings, load_categories
from leads.web import queries
from leads.web.queries import LeadFilter

STATIC = Path(__file__).parent / "static"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"

app = FastAPI(title="Lead dashboard", docs_url=None, redoc_url=None, openapi_url=None)
# Other sites must not be able to read the leads by pointing their own name at 127.0.0.1.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])


@app.middleware("http")
async def _headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # A page on another site can post here without reading the reply; browsers cannot add
    # this header cross-site without permission.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "leads":
        return Response("Forbidden", status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


DB = Annotated[Session, Depends(_session)]


def _filter(
    q: Annotated[str, Query(max_length=100)] = "",
    country: Annotated[str, Query(max_length=2)] = "",
    category: Annotated[str, Query(max_length=50)] = "",
    city: Annotated[str, Query(max_length=100)] = "",
    source: Annotated[str, Query(max_length=50)] = "",
    year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    age: Annotated[str, Query(max_length=10)] = "",
    email: Annotated[str, Query(max_length=3)] = "",
    phone: Annotated[str, Query(max_length=3)] = "",
    website: Annotated[str, Query(max_length=20)] = "",
) -> LeadFilter:
    return LeadFilter(q, country, category, city, source, year, age, email, phone, website)


Filter = Annotated[LeadFilter, Depends(_filter)]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview(db: DB) -> dict[str, Any]:
    return {**queries.overview(db), "troubles": queries.troubles(db)}


@app.get("/api/options")
def options(db: DB) -> dict[str, Any]:
    stats = queries.overview(db)
    return {
        "categories": load_categories().groups,
        "countries": [r["key"] for r in stats["countries"] if r["key"]],
        "years": [r["key"] for r in reversed(stats["years"])],
        "sources": [r["key"] for r in stats["sources"]],
    }


@app.get("/api/leads")
def leads(
    db: DB, f: Filter, sort: str = "newest", page: Annotated[int, Query(ge=1, le=100_000)] = 1
) -> dict[str, Any]:
    return queries.list_leads(db, f, sort, page)


def csv_cell(value: Any) -> str:
    """Text for one cell; a leading formula character is neutralised for spreadsheets."""
    if value is None:
        return ""
    cell = ";".join(map(str, value)) if isinstance(value, list) else str(value)
    if cell.startswith(("=", "+", "-", "@", "\t", "\r")) and not re.fullmatch(r"\+\d+", cell):
        return "'" + cell
    return cell


@app.get("/api/leads.csv")
def leads_csv(f: Filter, sort: str = "newest") -> StreamingResponse:
    def lines() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            out = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
            return out

        buffer.write("﻿")  # lets Excel read accented names
        writer.writerow(EXPORT_COLUMNS)
        with session_scope() as session:
            for row in queries.stream_leads(session, f, sort, EXPORT_COLUMNS):
                writer.writerow([csv_cell(row[c]) for c in EXPORT_COLUMNS])
                if buffer.tell() > 65_536:
                    yield flush()
        writer.writerow([])
        for line in ATTRIBUTION_LINES:
            writer.writerow([line])
        yield flush()

    return StreamingResponse(
        lines(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


@app.get("/api/leads/{lead_id}")
def lead(lead_id: str, db: DB) -> dict[str, Any]:
    data = queries.lead_detail(db, lead_id)
    if data is None:
        raise HTTPException(404, "Lead not found")
    data["suppressed"] = queries.is_suppressed(db, data["email"], data["phone_e164"])
    return data


def setup_state(settings: Settings) -> dict[str, bool]:
    sender = settings.sender
    return {
        "gmail": settings.token_path.is_file() and bool(settings.gmail_account),
        "ai": bool(settings.groq_api_key and settings.groq_api_key.get_secret_value()),
        "sender": not drafts.warnings(settings) and sender.company not in ("", "Your Company"),
        "sheet": bool(settings.google_sheet_id),
    }


@app.get("/api/leads/{lead_id}/email")
def email_panel(lead_id: str, db: DB) -> dict[str, Any]:
    lead_row = db.get(Lead, lead_id)
    if lead_row is None:
        raise HTTPException(404, "Lead not found")
    settings = get_settings()
    state = setup_state(settings)
    language = lead_language(lead_row.name, lead_row.country_iso2)
    saved = drafts.saved_email(db, lead_row)
    return {
        "to": lead_row.email,
        "language": LANGUAGES[language].name if language in LANGUAGES else None,
        "email": {"subject": saved.subject, "body": saved.body} if saved else None,
        "blockers": drafts.blockers(db, lead_row, settings),
        "warnings": drafts.warnings(settings),
        "ai": state["ai"],
        "gmail": state["gmail"],
        "drafted": any(
            o["drafted_at"] for o in (queries.lead_detail(db, lead_id) or {})["outreach"]
        ),
    }


@app.post("/api/leads/{lead_id}/email")
def write(lead_id: str, again: bool = False) -> dict[str, str]:
    try:
        email = drafts.generate(lead_id, again=again)
    except (drafts.DraftError, AiError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"subject": email.subject, "body": email.body}


class DraftText(BaseModel):
    subject: str = Field(max_length=300)
    body: str = Field(max_length=10_000)


@app.post("/api/leads/{lead_id}/draft", status_code=201)
def save_draft(lead_id: str, text: DraftText) -> dict[str, str]:
    try:
        draft_id = drafts.save_to_gmail(lead_id, text.subject, text.body)
    except (drafts.DraftError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    queries.clear_cache()
    return {"draft_id": draft_id}


@app.get("/api/outreach")
def outreach(db: DB) -> list[dict[str, Any]]:
    return queries.list_outreach(db)


@app.get("/api/suppression")
def suppression(db: DB) -> list[dict[str, Any]]:
    return queries.list_suppression(db)


class OptOut(BaseModel):
    contact: str


def normalise_contact(value: str) -> str | None:
    """Lower-cased email or E.164 phone, the two forms the suppression list stores."""
    value = value.strip()
    if _EMAIL.fullmatch(value):
        return value.lower()
    try:
        number = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


@app.post("/api/suppression", status_code=201)
def add_suppression(body: OptOut, db: DB) -> dict[str, str]:
    contact = normalise_contact(body.contact)
    if contact is None:
        raise HTTPException(422, "Enter an email, or a phone number starting with + and country")
    if db.get(Suppression, contact) is None:
        db.add(Suppression(email_or_phone=contact, reason="manual"))
        db.commit()  # the page reloads the list as soon as this call answers
        queries.clear_cache()
    return {"contact": contact}


@app.get("/api/setup")
def setup(db: DB) -> dict[str, Any]:
    settings = get_settings()
    stats = queries.overview(db)
    last = queries.last_runs(db)
    return {
        "state": setup_state(settings),
        "warnings": drafts.warnings(settings),
        "schedule": [
            {"name": name, "cron": cron, "last": last.get(name)}
            for name, cron in (
                ("gmail-sync", settings.schedules.gmail_sync),
                ("daily-drafts", settings.schedules.daily_drafts),
                ("monthly-collect", settings.schedules.monthly_collect),
                ("backup-check", settings.schedules.backup_check),
            )
        ],
        "timezone": settings.timezone,
        "auto_drafts": settings.outreach.auto_drafts,
        "countries": settings.collect.countries,
        "since": settings.since,
        "drafts_per_day": settings.batch.drafts_per_day,
        "followup_after_days": settings.outreach.followup_after_days,
        "include_unknown_age": settings.include_unknown_age,
        "checked": stats["totals"]["checked"],
        "total": stats["totals"]["total"],
        "last_checked": db.scalar(select(func.max(Lead.website_checked_at))),
        "sources": [
            {"name": name, "enabled": settings.source_enabled(name)} for name in settings.sources
        ],
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")
