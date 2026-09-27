"""
Pure, DB-free logic: given a transaction's full event history (in
event-timestamp order), compute its current payment_status,
settlement_status, and any *structural* discrepancy (i.e. one that's
evident from the event sequence itself, as opposed to a time-based one
like "processed but not settled for 24h" which needs a reference clock
and lives in services/reconciliation.py instead).

Kept as pure functions (no DB session) so it's trivially unit-testable.
"""

from dataclasses import dataclass, field

from app.models import EventType, PaymentStatus, SettlementStatus


@dataclass
class DerivedState:
    payment_status: PaymentStatus = PaymentStatus.unknown
    settlement_status: SettlementStatus = SettlementStatus.unsettled
    amount: float | None = None
    currency: str | None = None
    discrepancy_reasons: list[str] = field(default_factory=list)

    @property
    def has_discrepancy(self) -> bool:
        return len(self.discrepancy_reasons) > 0

    @property
    def discrepancy_reason_str(self) -> str | None:
        return "; ".join(self.discrepancy_reasons) if self.discrepancy_reasons else None


class EventLike:
    """Minimal duck-typed shape so tests can pass plain objects, not ORM rows."""

    event_type: EventType
    amount: float | None
    currency: str | None


def derive_transaction_state(events: list) -> DerivedState:
    """
    events: iterable of objects with .event_type, .amount, .currency,
    already sorted by event_timestamp ascending (caller's responsibility --
    keeping the sort out of here keeps this function pure/cheap to test).
    """
    state = DerivedState()
    seen_amount: float | None = None
    seen_currency: str | None = None
    has_initiated = False
    has_processed = False
    has_failed = False
    has_settled = False

    for ev in events:
        etype = ev.event_type

        # Track amount/currency consistency across events for this transaction.
        if ev.amount is not None:
            if seen_amount is not None and float(seen_amount) != float(ev.amount):
                reason = "conflicting_amount_across_events"
                if reason not in state.discrepancy_reasons:
                    state.discrepancy_reasons.append(reason)
            seen_amount = ev.amount
        if ev.currency is not None:
            if seen_currency is not None and seen_currency != ev.currency:
                reason = "conflicting_currency_across_events"
                if reason not in state.discrepancy_reasons:
                    state.discrepancy_reasons.append(reason)
            seen_currency = ev.currency

        if etype == EventType.payment_initiated:
            has_initiated = True
            state.payment_status = PaymentStatus.initiated

        elif etype == EventType.payment_processed:
            if has_failed:
                _flag(state, "processed_after_failure")
            if not has_initiated:
                _flag(state, "processed_without_initiation")
            has_processed = True
            state.payment_status = PaymentStatus.processed

        elif etype == EventType.payment_failed:
            if has_settled:
                _flag(state, "failure_after_settlement")
            has_failed = True
            state.payment_status = PaymentStatus.failed

        elif etype == EventType.settled:
            if has_failed:
                _flag(state, "settled_after_failure")
            elif not has_processed:
                _flag(state, "settled_without_processing")
            has_settled = True
            state.settlement_status = SettlementStatus.settled

    state.amount = seen_amount
    state.currency = seen_currency
    return state


def _flag(state: DerivedState, reason: str) -> None:
    if reason not in state.discrepancy_reasons:
        state.discrepancy_reasons.append(reason)
