from types import SimpleNamespace

from app.models import EventType, PaymentStatus, SettlementStatus
from app.services.state_machine import derive_transaction_state


def ev(event_type: EventType, amount: float = 100.0, currency: str = "INR"):
    return SimpleNamespace(event_type=event_type, amount=amount, currency=currency)


def test_happy_path_settled():
    events = [
        ev(EventType.payment_initiated),
        ev(EventType.payment_processed),
        ev(EventType.settled),
    ]
    state = derive_transaction_state(events)
    assert state.payment_status == PaymentStatus.processed
    assert state.settlement_status == SettlementStatus.settled
    assert not state.has_discrepancy


def test_failure_path():
    events = [ev(EventType.payment_initiated), ev(EventType.payment_failed)]
    state = derive_transaction_state(events)
    assert state.payment_status == PaymentStatus.failed
    assert state.settlement_status == SettlementStatus.unsettled
    assert not state.has_discrepancy


def test_settled_without_processing_is_flagged():
    events = [ev(EventType.settled)]
    state = derive_transaction_state(events)
    assert state.has_discrepancy
    assert "settled_without_processing" in state.discrepancy_reason_str


def test_settled_after_failure_is_flagged():
    events = [
        ev(EventType.payment_initiated),
        ev(EventType.payment_failed),
        ev(EventType.settled),
    ]
    state = derive_transaction_state(events)
    assert state.has_discrepancy
    assert "settled_after_failure" in state.discrepancy_reason_str


def test_conflicting_amount_across_events_is_flagged():
    events = [
        ev(EventType.payment_initiated, amount=100.0),
        ev(EventType.payment_processed, amount=150.0),
    ]
    state = derive_transaction_state(events)
    assert state.has_discrepancy
    assert "conflicting_amount_across_events" in state.discrepancy_reason_str


def test_pending_settlement_has_no_structural_discrepancy():
    # This is intentionally NOT flagged as a structural discrepancy --
    # "processed but not yet settled" only becomes discrepant after the SLA
    # window, which is evaluated separately in services/reconciliation.py.
    events = [ev(EventType.payment_initiated), ev(EventType.payment_processed)]
    state = derive_transaction_state(events)
    assert state.payment_status == PaymentStatus.processed
    assert state.settlement_status == SettlementStatus.unsettled
    assert not state.has_discrepancy
