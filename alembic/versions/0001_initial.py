"""initial schema: merchants, transactions, events

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("merchant_id", sa.String(64), primary_key=True),
        sa.Column("merchant_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.String(64), primary_key=True),
        sa.Column(
            "merchant_id",
            sa.String(64),
            sa.ForeignKey("merchants.merchant_id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("payment_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column(
            "settlement_status", sa.String(16), nullable=False, server_default="unsettled"
        ),
        sa.Column(
            "has_discrepancy", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("discrepancy_reason", sa.String(64), nullable=True),
        sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_transactions_merchant_status", "transactions", ["merchant_id", "payment_status"]
    )
    op.create_index("ix_transactions_last_event_at", "transactions", ["last_event_at"])
    op.create_index(
        "ix_transactions_discrepancy", "transactions", ["has_discrepancy", "settlement_status"]
    )

    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "transaction_id",
            sa.String(64),
            sa.ForeignKey("transactions.transaction_id"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON, nullable=False),
        sa.UniqueConstraint("event_id", name="uq_events_event_id"),
    )
    op.create_index("ix_events_transaction_id", "events", ["transaction_id"])
    op.create_index("ix_events_merchant_id", "events", ["merchant_id"])
    op.create_index("ix_events_event_timestamp", "events", ["event_timestamp"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("transactions")
    op.drop_table("merchants")
