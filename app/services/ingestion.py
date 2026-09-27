from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Event, EventType, Merchant, Transaction
from app.schemas import EventIn
from app.services.state_machine import derive_transaction_state


@dataclass
class IngestOutcome:
    status: str  # "accepted" | "duplicate_ignored"
    transaction: Transaction


def ingest_event(db: Session, payload: EventIn) -> IngestOutcome:
    """
    Idempotent single-event ingestion.

    Concurrency-safety note: uniqueness of event_id is enforced by a DB
    constraint (see models.Event.uq_events_event_id), not just the
    pre-check below. The pre-check is an optimization to avoid a wasted
    round trip on the common case; the try/except around commit is what
    actually guarantees correctness if two identical requests race.
    """
    existing = db.execute(
        select(Event).where(Event.event_id == payload.event_id)
    ).scalar_one_or_none()

    if existing is not None:
        txn = db.get(Transaction, existing.transaction_id)
        return IngestOutcome(status="duplicate_ignored", transaction=txn)

    _upsert_merchant(db, payload.merchant_id, payload.merchant_name)
    _ensure_transaction_row(db, payload.transaction_id, payload.merchant_id)

    event_row = Event(
        event_id=payload.event_id,
        event_type=EventType(payload.event_type.value),
        transaction_id=payload.transaction_id,
        merchant_id=payload.merchant_id,
        amount=payload.amount,
        currency=payload.currency,
        event_timestamp=payload.timestamp,
        raw_payload=payload.model_dump(mode="json"),
    )
    db.add(event_row)

    try:
        db.flush()  # surfaces the unique-constraint race here, inside the try
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(Event).where(Event.event_id == payload.event_id)
        ).scalar_one_or_none()
        txn = db.get(Transaction, existing.transaction_id) if existing else None
        return IngestOutcome(status="duplicate_ignored", transaction=txn)

    txn = _recompute_transaction_state(db, payload.transaction_id)
    db.commit()
    db.refresh(txn)
    return IngestOutcome(status="accepted", transaction=txn)


def _upsert_merchant(db: Session, merchant_id: str, merchant_name: str | None) -> None:
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        db.add(Merchant(merchant_id=merchant_id, merchant_name=merchant_name or merchant_id))
    elif merchant_name and merchant.merchant_name != merchant_name:
        # Later events may carry a corrected/canonical name; last-write-wins
        # is an acceptable, documented simplification here.
        merchant.merchant_name = merchant_name


def _ensure_transaction_row(db: Session, transaction_id: str, merchant_id: str) -> None:
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        db.add(Transaction(transaction_id=transaction_id, merchant_id=merchant_id))


def _recompute_transaction_state(db: Session, transaction_id: str) -> Transaction:
    events = (
        db.execute(
            select(Event)
            .where(Event.transaction_id == transaction_id)
            .order_by(Event.event_timestamp.asc(), Event.received_at.asc())
        )
        .scalars()
        .all()
    )
    derived = derive_transaction_state(events)

    txn = db.get(Transaction, transaction_id)
    txn.payment_status = derived.payment_status
    txn.settlement_status = derived.settlement_status
    if derived.amount is not None:
        txn.amount = derived.amount
    if derived.currency is not None:
        txn.currency = derived.currency
    txn.has_discrepancy = derived.has_discrepancy
    txn.discrepancy_reason = derived.discrepancy_reason_str
    txn.first_event_at = events[0].event_timestamp
    txn.last_event_at = events[-1].event_timestamp
    return txn
