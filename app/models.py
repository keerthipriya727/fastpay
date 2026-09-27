import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventType(str, enum.Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


class PaymentStatus(str, enum.Enum):
    initiated = "initiated"
    processed = "processed"
    failed = "failed"
    unknown = "unknown"


class SettlementStatus(str, enum.Enum):
    unsettled = "unsettled"
    settled = "settled"


class Merchant(Base):
    """
    Reference table. Upserted lazily the first time an event mentions a
    merchant_id we haven't seen before -- there's no separate "create merchant"
    API in scope, so this table is populated as a side effect of ingestion.
    """

    __tablename__ = "merchants"

    merchant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="merchant")


class Transaction(Base):
    """
    Derived/materialized current state of a transaction, rebuilt from its
    event history every time a new event for it is ingested. This table is
    what GET /transactions and GET /transactions/{id} read from -- it exists
    so those endpoints don't have to replay event history on every read.
    """

    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        ForeignKey("merchants.merchant_id"), nullable=False
    )

    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False), default=PaymentStatus.unknown
    )
    settlement_status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus, native_enum=False), default=SettlementStatus.unsettled
    )

    has_discrepancy: Mapped[bool] = mapped_column(default=False)
    discrepancy_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    first_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="transactions")
    events: Mapped[list["Event"]] = relationship(
        back_populates="transaction", order_by="Event.event_timestamp"
    )

    __table_args__ = (
        Index("ix_transactions_merchant_status", "merchant_id", "payment_status"),
        Index("ix_transactions_last_event_at", "last_event_at"),
        Index(
            "ix_transactions_discrepancy", "has_discrepancy", "settlement_status"
        ),
    )


class Event(Base):
    """
    Append-only, immutable source of truth. Never updated or deleted after
    insert. transaction_id/merchant_id are plain columns (not hard FKs to
    keep ingestion order-independent) plus we backfill the FK-like link once
    the parent Transaction row exists in the same insert transaction.
    """

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # This is the idempotency key. The unique constraint below is what makes
    # duplicate submission safe -- enforced by the DB, not app logic.
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    event_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False))
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.transaction_id"), nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    raw_payload: Mapped[dict] = mapped_column(JSON)

    transaction: Mapped["Transaction"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_events_event_id"),
        Index("ix_events_transaction_id", "transaction_id"),
        Index("ix_events_merchant_id", "merchant_id"),
        Index("ix_events_event_timestamp", "event_timestamp"),
    )
