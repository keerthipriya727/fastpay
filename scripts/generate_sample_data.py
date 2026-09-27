"""
Generates sample_events.json: a realistic mix of payment lifecycle events
across multiple merchants, deliberately including the edge cases the
service is meant to demonstrate handling of:

  - happy path: initiated -> processed -> settled
  - failure path: initiated -> failed
  - pending settlement: initiated -> processed (no settled event yet, some
    old enough to breach the SLA, some too recent to count as late)
  - true duplicates: the exact same event_id submitted twice (idempotency)
  - semantic duplicates: a processed event resent with a *new* event_id
    (simulates an upstream retry without idempotency key) -- occasionally
    with a conflicting amount, to exercise conflicting_amount_across_events
  - structural discrepancies: settled with no processed, settled after a
    failure, processed after a failure
  - out-of-order delivery: events for the same transaction shuffled so
    received order != event_timestamp order

Pure stdlib. Deterministic given SEED, so re-running reproduces the same
file (useful for grading reproducibility).
"""
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 42
random.seed(SEED)

MERCHANTS = [
    ("merchant_1", "UrbanMart"),
    ("merchant_2", "FreshBasket"),
    ("merchant_3", "QuickCart"),
    ("merchant_4", "StyleHub"),
    ("merchant_5", "GadgetZone"),
]

CURRENCY = "INR"
TARGET_TOTAL_EVENTS = 10500
NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)  # fixed reference "now" for the demo


def rand_amount() -> float:
    return round(random.uniform(50, 50000), 2)


def rand_past_timestamp(days_back_max: int, days_back_min: int = 0) -> datetime:
    delta_seconds = random.randint(days_back_min * 86400, days_back_max * 86400)
    return NOW - timedelta(seconds=delta_seconds)


def make_event(event_type: str, transaction_id: str, merchant_id: str, merchant_name: str,
                amount: float, ts: datetime, event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "transaction_id": transaction_id,
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "amount": amount,
        "currency": CURRENCY,
        "timestamp": ts.isoformat(),
    }


def build_events() -> list[dict]:
    events: list[dict] = []
    txn_count = 0

    def new_txn_id() -> str:
        nonlocal txn_count
        txn_count += 1
        return str(uuid.uuid4())

    # Scenario weights (roughly). We loop transactions until we clear the
    # event-count target, since each scenario emits a different # of events.
    scenarios = (
        ["happy_settled"] * 45
        + ["failed"] * 20
        + ["pending_recent"] * 8
        + ["pending_sla_breach"] * 10
        + ["duplicate_exact"] * 5
        + ["duplicate_semantic"] * 5
        + ["duplicate_semantic_conflict"] * 2
        + ["settled_without_processing"] * 2
        + ["settled_after_failure"] * 2
        + ["processed_after_failure"] * 1
    )

    while len(events) < TARGET_TOTAL_EVENTS:
        scenario = random.choice(scenarios)
        merchant_id, merchant_name = random.choice(MERCHANTS)
        txn_id = new_txn_id()
        amount = rand_amount()
        txn_events: list[dict] = []

        if scenario == "happy_settled":
            t0 = rand_past_timestamp(60, 2)
            t1 = t0 + timedelta(minutes=random.randint(1, 30))
            t2 = t1 + timedelta(minutes=random.randint(5, 120))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1),
                make_event("settled", txn_id, merchant_id, merchant_name, amount, t2),
            ]

        elif scenario == "failed":
            t0 = rand_past_timestamp(60, 2)
            t1 = t0 + timedelta(minutes=random.randint(1, 15))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_failed", txn_id, merchant_id, merchant_name, amount, t1),
            ]

        elif scenario == "pending_recent":
            # processed within the last few hours -> not yet an SLA breach
            t0 = rand_past_timestamp(1, 0) - timedelta(hours=2)
            t1 = t0 + timedelta(minutes=random.randint(1, 30))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1),
            ]

        elif scenario == "pending_sla_breach":
            # processed well over 24h ago, still no settled event
            t0 = rand_past_timestamp(30, 3)
            t1 = t0 + timedelta(minutes=random.randint(1, 30))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1),
            ]

        elif scenario == "duplicate_exact":
            t0 = rand_past_timestamp(60, 2)
            t1 = t0 + timedelta(minutes=random.randint(1, 30))
            initiated = make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0)
            processed = make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1)
            # Same event_id submitted twice, simulating an upstream retry.
            txn_events = [initiated, processed, dict(processed)]

        elif scenario == "duplicate_semantic":
            # Same semantic event, but source resent it with a fresh
            # event_id (no idempotency key upstream) and the same amount.
            t0 = rand_past_timestamp(60, 2)
            t1 = t0 + timedelta(minutes=random.randint(1, 30))
            t1b = t1 + timedelta(seconds=random.randint(5, 120))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1b),
            ]

        elif scenario == "duplicate_semantic_conflict":
            # Same as above but the resent copy disagrees on amount --
            # exercises conflicting_amount_across_events.
            t0 = rand_past_timestamp(60, 2)
            t1 = t0 + timedelta(minutes=random.randint(1, 30))
            t1b = t1 + timedelta(seconds=random.randint(5, 120))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t1),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, round(amount + 1, 2), t1b),
            ]

        elif scenario == "settled_without_processing":
            t0 = rand_past_timestamp(60, 2)
            txn_events = [
                make_event("settled", txn_id, merchant_id, merchant_name, amount, t0),
            ]

        elif scenario == "settled_after_failure":
            t0 = rand_past_timestamp(60, 5)
            t1 = t0 + timedelta(minutes=random.randint(1, 15))
            t2 = t1 + timedelta(minutes=random.randint(60, 500))  # settlement erroneously recorded later
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_failed", txn_id, merchant_id, merchant_name, amount, t1),
                make_event("settled", txn_id, merchant_id, merchant_name, amount, t2),
            ]

        elif scenario == "processed_after_failure":
            t0 = rand_past_timestamp(60, 5)
            t1 = t0 + timedelta(minutes=random.randint(1, 15))
            t2 = t1 + timedelta(minutes=random.randint(1, 30))
            txn_events = [
                make_event("payment_initiated", txn_id, merchant_id, merchant_name, amount, t0),
                make_event("payment_failed", txn_id, merchant_id, merchant_name, amount, t1),
                make_event("payment_processed", txn_id, merchant_id, merchant_name, amount, t2),
            ]

        # Out-of-order delivery: shuffle how events land in the file
        # (arrival order) while their timestamp fields keep the true order.
        random.shuffle(txn_events)
        events.extend(txn_events)

    return events[:TARGET_TOTAL_EVENTS] if len(events) > TARGET_TOTAL_EVENTS else events


def main() -> None:
    events = build_events()
    # Shuffle globally too, so events from different transactions/merchants
    # interleave the way a real multi-source feed would.
    random.shuffle(events)

    out_path = Path(__file__).resolve().parent.parent / "sample_events.json"
    out_path.write_text(json.dumps(events, indent=2))

    merchants_seen = {e["merchant_id"] for e in events}
    print(f"Wrote {len(events)} events across {len(merchants_seen)} merchants to {out_path}")


if __name__ == "__main__":
    main()
