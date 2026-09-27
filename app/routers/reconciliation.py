import math
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_api_key
from app.models import Transaction
from app.schemas import (
    DiscrepancyRow,
    PaginatedDiscrepancies,
    ReconciliationGroupRow,
    ReconciliationSummaryResponse,
)
from app.services import reconciliation as recon

router = APIRouter(
    prefix="/reconciliation", tags=["reconciliation"], dependencies=[Depends(require_api_key)]
)


def _as_utc(dt: datetime | None) -> datetime | None:
    """
    Normalizes to a timezone-aware UTC datetime before comparison.

    Postgres (the intended production DB, via TIMESTAMPTZ) round-trips
    timezone info correctly. 
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@router.get("/summary", response_model=ReconciliationSummaryResponse)
def reconciliation_summary(
    group_by: Literal["merchant", "date", "status"] = Query(default="merchant"),
    db: Session = Depends(get_db),
) -> ReconciliationSummaryResponse:
    rows = recon.summary(db, group_by)
    return ReconciliationSummaryResponse(
        group_by=group_by,
        generated_at=datetime.now(timezone.utc),
        rows=[ReconciliationGroupRow(**r) for r in rows],
    )


@router.get("/discrepancies", response_model=PaginatedDiscrepancies)
def reconciliation_discrepancies(
    merchant_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
) -> PaginatedDiscrepancies:
    """
    Union of structural discrepancies (bad event-sequence, flagged at
    ingest time) and the time-based SLA breach (processed-but-unsettled for
    too long, evaluated against the reference clock at request time).
    """
    discrepant_filter, _sla_breach_expr = recon.discrepancies_query_filters()

    filters = [discrepant_filter]
    if merchant_id:
        filters.append(Transaction.merchant_id == merchant_id)

    count_stmt = select(func.count()).select_from(Transaction).where(*filters)
    total = db.execute(count_stmt).scalar_one()

    stmt = (
        select(Transaction)
        .where(*filters)
        .order_by(Transaction.last_event_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    txns = db.execute(stmt).scalars().all()
    cutoff = recon.sla_cutoff()

    items = []
    for txn in txns:
        last_event_at = _as_utc(txn.last_event_at)
        breach = (
            txn.payment_status.value == "processed"
            and txn.settlement_status.value == "unsettled"
            and last_event_at is not None
            and last_event_at < cutoff
        )
        items.append(
            DiscrepancyRow(
                transaction_id=txn.transaction_id,
                merchant_id=txn.merchant_id,
                payment_status=txn.payment_status.value,
                settlement_status=txn.settlement_status.value,
                discrepancy_reason=recon.effective_discrepancy_reason(txn, breach),
                amount=txn.amount,
                last_event_at=txn.last_event_at,
            )
        )

    total_pages = math.ceil(total / page_size) if total else 0
    return PaginatedDiscrepancies(
        items=items, total=total, page=page, page_size=page_size, total_pages=total_pages
    )
