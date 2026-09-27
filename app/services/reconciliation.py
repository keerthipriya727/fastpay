from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PaymentStatus, SettlementStatus, Transaction

# The one time-based discrepancy that can't be precomputed at ingest time:
# a transaction that reached "processed" and simply never got a settled
# event within a business-defined SLA. It only becomes true as time passes
# with *no* new event, so it must be evaluated at query time.
TIME_BASED_DISCREPANCY = "processed_not_settled_within_sla"


def reference_time() -> datetime:
    settings = get_settings()
    if settings.reconciliation_reference_time:
        return datetime.fromisoformat(settings.reconciliation_reference_time)
    return datetime.now(timezone.utc)


def sla_cutoff() -> datetime:
    """The instant before which an unsettled 'processed' transaction is late."""
    threshold = timedelta(hours=get_settings().unsettled_threshold_hours)
    return reference_time() - threshold


def summary(db: Session, group_by: str) -> list[dict]:
    """
    GET /reconciliation/summary — aggregate counts/amounts grouped by
    merchant, date, or status. One query per group_by, each backed by the
    indexes on (merchant_id, payment_status) / last_event_at.
    """
    settled_amount_expr = func.coalesce(
        func.sum(
            case((Transaction.settlement_status == SettlementStatus.settled, Transaction.amount), else_=0)
        ),
        0,
    )
    unsettled_amount_expr = func.coalesce(
        func.sum(
            case((Transaction.settlement_status == SettlementStatus.unsettled, Transaction.amount), else_=0)
        ),
        0,
    )
    settled_count_expr = func.coalesce(
        func.sum(case((Transaction.settlement_status == SettlementStatus.settled, 1), else_=0)), 0
    )
    unsettled_count_expr = func.coalesce(
        func.sum(case((Transaction.settlement_status == SettlementStatus.unsettled, 1), else_=0)), 0
    )
    discrepancy_count_expr = func.coalesce(
        func.sum(case((Transaction.has_discrepancy.is_(True), 1), else_=0)), 0
    )

    if group_by == "merchant":
        key_col = Transaction.merchant_id
    elif group_by == "status":
        key_col = Transaction.payment_status
    elif group_by == "date":
        key_col = func.date(Transaction.last_event_at)
    else:
        raise ValueError(f"unsupported group_by: {group_by}")

    stmt = (
        select(
            key_col.label("group_key"),
            func.count(Transaction.transaction_id).label("total_transactions"),
            func.coalesce(func.sum(Transaction.amount), 0).label("total_amount"),
            settled_count_expr.label("settled_count"),
            settled_amount_expr.label("settled_amount"),
            unsettled_count_expr.label("unsettled_count"),
            unsettled_amount_expr.label("unsettled_amount"),
            discrepancy_count_expr.label("discrepancy_count"),
        )
        .group_by(key_col)
        .order_by(key_col)
    )
    rows = db.execute(stmt).all()
    return [dict(r._mapping) | {"group_key": _stringify_key(r.group_key)} for r in rows]


def _stringify_key(value) -> str:
    # payment_status comes back as a PaymentStatus enum member when
    # group_by="status"; use .value ("processed") rather than str() (which
    # for a str-mixin Enum can render as "PaymentStatus.processed"
    # depending on Python version). merchant_id/date group keys are
    # already plain strings.
    return value.value if hasattr(value, "value") else str(value)


def discrepancies_query_filters():
    """
    Returns a SQLAlchemy selectable of transaction_ids that are discrepant,
    combining:
      1. structural discrepancies (has_discrepancy, computed at ingest time)
      2. the time-based SLA breach (computed here, against the reference clock)

    Kept as a filter-builder (not a full endpoint function) so both the
    paginated list endpoint and any future "count only" endpoint can reuse
    the same definition of "discrepant" without drifting apart.

    Known limitation: this comparison is pushed into SQL, which is exactly
    right for Postgres (TIMESTAMPTZ compares correctly). SQLite does not
    store true timezone-aware timestamps, so this same query can be
    unreliable there. Since SQLite is documented (README) as a local/demo
    convenience only -- not the production target -- this is accepted
    rather than worked around with extra complexity here.
    """
    cutoff = sla_cutoff()

    sla_breach = (
        (Transaction.payment_status == PaymentStatus.processed)
        & (Transaction.settlement_status == SettlementStatus.unsettled)
        & (Transaction.last_event_at.is_not(None))
        & (Transaction.last_event_at < cutoff)
    )
    structural = Transaction.has_discrepancy.is_(True)
    return structural | sla_breach, sla_breach


def effective_discrepancy_reason(txn: Transaction, sla_breach: bool) -> str:
    reasons = []
    if txn.discrepancy_reason:
        reasons.append(txn.discrepancy_reason)
    if sla_breach:
        reasons.append(TIME_BASED_DISCREPANCY)
    return "; ".join(reasons) if reasons else "unknown"
