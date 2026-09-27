from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_api_key
from app.schemas import BatchIngestResponse, EventIn, EventIngestResult
from app.services.ingestion import ingest_event

router = APIRouter(prefix="/events", tags=["events"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=EventIngestResult, status_code=201)
def ingest_single_event(payload: EventIn, db: Session = Depends(get_db)) -> EventIngestResult:
    """
    Ingest one payment lifecycle event. Idempotent on event_id: resubmitting
    the same event_id is a no-op (200-shape response with status
    'duplicate_ignored'), never a 4xx -- retries from an upstream system are
    an expected, normal case for a webhook-style endpoint, not an error.
    """
    outcome = ingest_event(db, payload)
    return EventIngestResult(
        event_id=payload.event_id,
        status=outcome.status,
        transaction_id=outcome.transaction.transaction_id,
        transaction_payment_status=outcome.transaction.payment_status.value,
    )


@router.post("/batch", response_model=BatchIngestResponse, status_code=201)
def ingest_batch(payloads: list[dict], db: Session = Depends(get_db)) -> BatchIngestResponse:
    """
    Batch ingestion for bulk/backfill loading (e.g. sample_events.json).
    Each event is validated and applied independently: one malformed event
    in a 10,000-event file shouldn't fail the other 9,999. Rejects are
    reported back with the original index and the validation error so the
    caller can fix and resubmit just those.
    """
    results: list[EventIngestResult] = []
    rejected: list[dict] = []

    for idx, raw in enumerate(payloads):
        try:
            payload = EventIn.model_validate(raw)
        except ValidationError as exc:
            rejected.append({"index": idx, "raw": raw, "error": exc.errors()})
            continue

        outcome = ingest_event(db, payload)
        results.append(
            EventIngestResult(
                event_id=payload.event_id,
                status=outcome.status,
                transaction_id=outcome.transaction.transaction_id,
                transaction_payment_status=outcome.transaction.payment_status.value,
            )
        )

    accepted = sum(1 for r in results if r.status == "accepted")
    duplicate = sum(1 for r in results if r.status == "duplicate_ignored")

    return BatchIngestResponse(
        results=results,
        accepted_count=accepted,
        duplicate_count=duplicate,
        rejected_count=len(rejected),
        rejected=rejected,
    )
