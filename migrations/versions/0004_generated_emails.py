"""AI-written emails, and at most one first email and one follow-up per lead.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generated_emails",
        sa.Column(
            "lead_id",
            sa.Text(),
            sa.ForeignKey("leads.lead_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("website_status", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("outreach", sa.Column("recipient", sa.Text()))
    op.add_column("outreach", sa.Column("is_followup", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.execute("UPDATE outreach SET is_followup = (stage = 'followup')")
    op.create_index(
        "uq_outreach_lead_message", "outreach", ["lead_id", "channel", "is_followup"], unique=True
    )
    # One first email per address, even when several leads share it.
    op.create_index(
        "uq_outreach_recipient",
        "outreach",
        ["recipient"],
        unique=True,
        postgresql_where=sa.text("recipient IS NOT NULL AND NOT is_followup"),
    )


def downgrade() -> None:
    op.drop_index("uq_outreach_recipient", "outreach")
    op.drop_index("uq_outreach_lead_message", "outreach")
    op.drop_column("outreach", "is_followup")
    op.drop_column("outreach", "recipient")
    op.drop_table("generated_emails")
