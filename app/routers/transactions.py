import math
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.dependencies import require_api_key
from app.models import Merchant, PaymentStatus, Transaction
from app.schemas import PaginatedTransactions, TransactionDetail, TransactionSummary

router = APIRouter(
    prefix="/transactions", tags=["transactions"], dependencies=[Depends(require_api_key)]
)

SORTABLE_FIELDS = {
    "last_event_at": Transaction.last_event_at,
    "first_event_at": Transaction.first_event_at,
    "amount": Transaction.amount,
    "created_at": Transaction.created_at,
}


@router.get("", response_model=PaginatedTransactions)
def list_transactions(
    merchant_id: str | None = Query(default=None),
    status_filter: PaymentStatus | None = Query(default=None, alias="status"),
    date_from: datetime | None = Query(default=None, description="Filter on last_event_at >="),
    date_to: datetime | None = Query(default=None, description="Filter on last_event_at <="),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    sort_by: Literal["last_event_at", "first_event_at", "amount", "created_at"] = "last_event_at",
    sort_order: Literal["asc", "desc"] = "desc",
    db: Session = Depends(get_db),
) -> PaginatedTransactions:
    """
    Filter by merchant / status / date range, with offset pagination and
    sorting. Offset pagination chosen over cursor-based for simplicity and
    ease of manual testing (page numbers) at this data volume; cursor-based
    pagination would be the better choice at high write concurrency, where
    offset pages can skip/duplicate rows as new transactions are inserted.
    """
    filters = []
    if merchant_id:
        filters.append(Transaction.merchant_id == merchant_id)
    if status_filter:
        filters.append(Transaction.payment_status == status_filter)
    if date_from:
        filters.append(Transaction.last_event_at >= date_from)
    if date_to:
        filters.append(Transaction.last_event_at <= date_to)

    stmt = select(Transaction).where(*filters)
    # COUNT(*) pushed down to the DB rather than fetching rows to count them
    # in Python -- matters once this table is bigger than a demo dataset.
    count_stmt = select(func.count()).select_from(Transaction).where(*filters)
    total = db.execute(count_stmt).scalar_one()

    sort_col = SORTABLE_FIELDS[sort_by]
    sort_col = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    stmt = stmt.order_by(sort_col).offset((page - 1) * page_size).limit(page_size)

    items = db.execute(stmt).scalars().all()
    total_pages = math.ceil(total / page_size) if total else 0

    return PaginatedTransactions(
        items=[TransactionSummary.model_validate(t) for t in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{transaction_id}", response_model=TransactionDetail)
def get_transaction(transaction_id: str, db: Session = Depends(get_db)) -> TransactionDetail:
    stmt = (
        select(Transaction)
        .where(Transaction.transaction_id == transaction_id)
        .options(selectinload(Transaction.events))
    )
    txn = db.execute(stmt).scalar_one_or_none()
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    merchant = db.get(Merchant, txn.merchant_id)
    detail = TransactionDetail.model_validate(txn)
    detail.merchant_name = merchant.merchant_name if merchant else None
    detail.events = sorted(detail.events, key=lambda e: e.event_timestamp)
    return detail
