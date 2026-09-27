from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------- Enums mirrored for the API boundary ----------

class EventTypeSchema(str, Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


# ---------- Ingestion ----------

class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: EventTypeSchema
    transaction_id: str = Field(..., min_length=1, max_length=64)
    merchant_id: str = Field(..., min_length=1, max_length=64)
    merchant_name: str | None = Field(None, max_length=255)
    amount: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, min_length=3, max_length=8)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include timezone info (e.g. +00:00 / Z)")
        return v


class EventIngestResult(BaseModel):
    event_id: str
    status: Literal["accepted", "duplicate_ignored"]
    transaction_id: str
    transaction_payment_status: str


class BatchIngestResponse(BaseModel):
    results: list[EventIngestResult]
    accepted_count: int
    duplicate_count: int
    rejected_count: int
    rejected: list[dict[str, Any]] = Field(default_factory=list)


# ---------- Retrieval ----------

class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    event_type: EventTypeSchema
    amount: Decimal | None
    currency: str | None
    event_timestamp: datetime
    received_at: datetime


class TransactionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    amount: Decimal | None
    currency: str | None
    payment_status: str
    settlement_status: str
    has_discrepancy: bool
    first_event_at: datetime | None
    last_event_at: datetime | None


class TransactionDetail(TransactionSummary):
    merchant_name: str | None = None
    discrepancy_reason: str | None = None
    events: list[EventOut] = Field(default_factory=list)


class PaginatedTransactions(BaseModel):
    items: list[TransactionSummary]
    total: int
    page: int
    page_size: int
    total_pages: int


# ---------- Reconciliation ----------

class ReconciliationGroupRow(BaseModel):
    group_key: str
    total_transactions: int
    total_amount: Decimal
    settled_count: int
    settled_amount: Decimal
    unsettled_count: int
    unsettled_amount: Decimal
    discrepancy_count: int


class ReconciliationSummaryResponse(BaseModel):
    group_by: Literal["merchant", "date", "status"]
    generated_at: datetime
    rows: list[ReconciliationGroupRow]


class DiscrepancyRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    payment_status: str
    settlement_status: str
    discrepancy_reason: str
    amount: Decimal | None
    last_event_at: datetime | None


class PaginatedDiscrepancies(BaseModel):
    items: list[DiscrepancyRow]
    total: int
    page: int
    page_size: int
    total_pages: int
