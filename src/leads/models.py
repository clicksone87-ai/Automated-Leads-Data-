"""SQLAlchemy models. Keep in sync with Alembic migrations in ``migrations/versions``."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType

# --- enums (stored as plain text) ------------------------------------------------------


class WebsiteStatus(StrEnum):
    WORKING = "working"
    BROKEN = "broken"
    PARKED = "parked"
    REDIRECT_SOCIAL = "redirect_social"
    NONE = "none"


class LeadStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class Stage(StrEnum):
    FIRST = "first"
    FOLLOWUP = "followup"
    REPLIED = "replied"
    OPTED_OUT = "opted_out"


class Channel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"


# --- PostGIS geometry without an extra dependency --------------------------------------


class Geometry(UserDefinedType[str]):
    """Minimal PostGIS geometry type. Values are EWKT strings, e.g. ``SRID=4326;POINT(lon lat)``."""

    cache_ok = True

    def __init__(self, geometry_type: str = "POINT", srid: int = 4326) -> None:
        self.geometry_type = geometry_type
        self.srid = srid

    def get_col_spec(self, **kw: Any) -> str:
        return f"geometry({self.geometry_type},{self.srid})"

    def bind_expression(self, bindvalue: Any) -> ColumnElement[str]:
        return func.ST_GeomFromEWKT(bindvalue, type_=self)

    def column_expression(self, colexpr: Any) -> ColumnElement[str]:
        return func.ST_AsEWKT(colexpr, type_=self)


def point_ewkt(lon: float, lat: float, srid: int = 4326) -> str:
    return f"SRID={srid};POINT({lon} {lat})"


# --- models ------------------------------------------------------------------------------


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
        datetime: DateTime(timezone=True),
        str: Text,
    }


class Lead(Base):
    __tablename__ = "leads"

    lead_id: Mapped[str] = mapped_column(primary_key=True)  # stable hash, see pipeline.match
    name: Mapped[str]
    category: Mapped[str | None]
    category_group: Mapped[str | None]
    phone_e164: Mapped[str | None]
    phone_type: Mapped[str | None]
    email: Mapped[str | None]
    website: Mapped[str | None]
    domain: Mapped[str | None]
    website_status: Mapped[str | None]  # WebsiteStatus; NULL = not checked yet
    website_reason: Mapped[str | None]  # what the check saw, e.g. "http 404"
    website_checked_at: Mapped[datetime | None]
    whatsapp_number: Mapped[str | None]
    whatsapp_evidence: Mapped[str | None]  # URL of the page that shows the number
    socials: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    address: Mapped[str | None]
    city: Mapped[str | None]
    region: Mapped[str | None]
    postcode: Mapped[str | None]
    country_iso2: Mapped[str | None] = mapped_column(String(2))
    geom: Mapped[str | None] = mapped_column(Geometry("POINT", 4326))
    created_date: Mapped[date | None]
    age_source: Mapped[str | None]
    age_unknown: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[str] = mapped_column(server_default=text("'active'"))  # LeadStatus
    sources: Mapped[list[str]] = mapped_column(server_default=text("'{}'::text[]"))
    first_seen: Mapped[datetime | None]
    last_seen: Mapped[datetime | None]
    lawful_basis_note: Mapped[str | None]  # set when the lead is first written to
    sanctions_hit: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    source_records: Mapped[list[LeadSource]] = relationship(back_populates="lead")
    outreach: Mapped[list[Outreach]] = relationship(back_populates="lead")

    __table_args__ = (
        Index("ix_leads_domain", "domain"),
        Index("ix_leads_phone_e164", "phone_e164"),
        Index("ix_leads_country_category", "country_iso2", "category_group"),
        Index("ix_leads_website_status", "website_status"),
        Index("ix_leads_geom", "geom", postgresql_using="gist"),
    )


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    flow: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default=text("'running'"))
    counts: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class LeadSource(Base):
    __tablename__ = "lead_sources"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.lead_id", ondelete="CASCADE"))
    source: Mapped[str]
    source_record_id: Mapped[str]
    raw: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.run_id", ondelete="SET NULL")
    )

    lead: Mapped[Lead] = relationship(back_populates="source_records")

    __table_args__ = (
        UniqueConstraint("source", "source_record_id", name="uq_lead_sources_source_record"),
        Index("ix_lead_sources_lead_id", "lead_id"),
    )


class Outreach(Base):
    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.lead_id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(server_default=text("'email'"))  # Channel
    stage: Mapped[str]  # Stage
    is_followup: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    recipient: Mapped[str | None]  # lower-cased address the message is for
    gmail_draft_id: Mapped[str | None]
    gmail_thread_id: Mapped[str | None]
    drafted_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    replied_at: Mapped[datetime | None]
    bounced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    opted_out: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    followup_due_at: Mapped[datetime | None]

    lead: Mapped[Lead] = relationship(back_populates="outreach")

    __table_args__ = (
        Index("ix_outreach_lead_id", "lead_id"),
        Index("uq_outreach_lead_message", "lead_id", "channel", "is_followup", unique=True),
        Index(
            "uq_outreach_recipient",
            "recipient",
            unique=True,
            postgresql_where=text("recipient IS NOT NULL AND NOT is_followup"),
        ),
        Index("ix_outreach_gmail_thread_id", "gmail_thread_id"),
    )


class GeneratedEmail(Base):
    """Latest AI-written first email of a lead."""

    __tablename__ = "generated_emails"

    lead_id: Mapped[str] = mapped_column(
        ForeignKey("leads.lead_id", ondelete="CASCADE"), primary_key=True
    )
    subject: Mapped[str]
    body: Mapped[str]
    model: Mapped[str]
    website_status: Mapped[str]  # the status the text was written for
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DomainAge(Base):
    """Wayback Machine lookups, kept so each domain is asked about only once."""

    __tablename__ = "domain_ages"

    domain: Mapped[str] = mapped_column(primary_key=True)
    first_capture: Mapped[date | None]  # NULL = never archived
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Suppression(Base):
    __tablename__ = "suppression"

    email_or_phone: Mapped[str] = mapped_column(primary_key=True)  # lower-cased email or E.164
    reason: Mapped[str]  # opt_out | hard_bounce | manual
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())
