"""Initial schema: leads, lead_sources, outreach, suppression, runs.

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("flow", sa.Text(), nullable=False),
        sa.Column("started_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("counts", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )

    op.create_table(
        "leads",
        sa.Column("lead_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text()),
        sa.Column("category_group", sa.Text()),
        sa.Column("phone_e164", sa.Text()),
        sa.Column("phone_type", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("website", sa.Text()),
        sa.Column("domain", sa.Text()),
        sa.Column("website_status", sa.Text()),
        sa.Column("website_checked_at", TS),
        sa.Column("whatsapp_number", sa.Text()),
        sa.Column("whatsapp_evidence", sa.Text()),
        sa.Column("socials", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("address", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("region", sa.Text()),
        sa.Column("postcode", sa.Text()),
        sa.Column("country_iso2", sa.String(2)),
        sa.Column("created_date", sa.Date()),
        sa.Column("age_source", sa.Text()),
        sa.Column("age_unknown", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "sources", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column("first_seen", TS),
        sa.Column("last_seen", TS),
        sa.Column("sanctions_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "website_status IS NULL OR website_status IN "
            "('working','broken','parked','redirect_social','none')",
            name="ck_leads_website_status",
        ),
        sa.CheckConstraint("status IN ('active','closed')", name="ck_leads_status"),
    )
    op.execute("ALTER TABLE leads ADD COLUMN geom geometry(Point, 4326)")
    op.create_index("ix_leads_domain", "leads", ["domain"])
    op.create_index("ix_leads_phone_e164", "leads", ["phone_e164"])
    op.create_index("ix_leads_country_category", "leads", ["country_iso2", "category_group"])
    op.create_index("ix_leads_website_status", "leads", ["website_status"])
    op.create_index("ix_leads_geom", "leads", ["geom"], postgresql_using="gist")

    op.create_table(
        "lead_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "lead_id", sa.Text(), sa.ForeignKey("leads.lead_id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("fetched_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.run_id", ondelete="SET NULL")
        ),
        sa.UniqueConstraint("source", "source_record_id", name="uq_lead_sources_source_record"),
    )
    op.create_index("ix_lead_sources_lead_id", "lead_sources", ["lead_id"])

    op.create_table(
        "outreach",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "lead_id", sa.Text(), sa.ForeignKey("leads.lead_id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("channel", sa.Text(), server_default=sa.text("'email'"), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("gmail_draft_id", sa.Text()),
        sa.Column("gmail_thread_id", sa.Text()),
        sa.Column("drafted_at", TS),
        sa.Column("sent_at", TS),
        sa.Column("replied_at", TS),
        sa.Column("bounced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("opted_out", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("followup_due_at", TS),
        sa.CheckConstraint("channel IN ('email','whatsapp')", name="ck_outreach_channel"),
        sa.CheckConstraint(
            "stage IN ('first','followup','replied','opted_out')", name="ck_outreach_stage"
        ),
    )
    op.create_index("ix_outreach_lead_id", "outreach", ["lead_id"])
    op.create_index("ix_outreach_gmail_thread_id", "outreach", ["gmail_thread_id"])

    op.create_table(
        "suppression",
        sa.Column("email_or_phone", sa.Text(), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("added_at", TS, server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("suppression")
    op.drop_table("outreach")
    op.drop_table("lead_sources")
    op.drop_table("leads")
    op.drop_table("runs")
