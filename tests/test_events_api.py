def sample_event(event_id="evt-1", event_type="payment_initiated", **overrides):
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "transaction_id": "txn-1",
        "merchant_id": "merchant_1",
        "merchant_name": "UrbanMart",
        "amount": 100.50,
        "currency": "INR",
        "timestamp": "2026-01-10T10:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_ingest_single_event_creates_transaction(client):
    resp = client.post("/events", json=sample_event())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["transaction_payment_status"] == "initiated"


def test_duplicate_event_id_is_idempotent(client):
    payload = sample_event()
    first = client.post("/events", json=payload)
    second = client.post("/events", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate_ignored"

    # And critically: transaction state wasn't double-applied.
    txn = client.get("/transactions/txn-1").json()
    assert len(txn["events"]) == 1


def test_out_of_order_events_resolve_to_correct_final_state(client):
    # Send 'settled' before 'payment_processed' before 'payment_initiated' --
    # arrival order is reversed relative to their timestamps.
    client.post(
        "/events",
        json=sample_event(
            event_id="e3", event_type="settled", timestamp="2026-01-10T10:10:00+00:00"
        ),
    )
    client.post(
        "/events",
        json=sample_event(
            event_id="e2",
            event_type="payment_processed",
            timestamp="2026-01-10T10:05:00+00:00",
        ),
    )
    client.post(
        "/events",
        json=sample_event(
            event_id="e1",
            event_type="payment_initiated",
            timestamp="2026-01-10T10:00:00+00:00",
        ),
    )

    txn = client.get("/transactions/txn-1").json()
    assert txn["payment_status"] == "processed"
    assert txn["settlement_status"] == "settled"
    assert txn["has_discrepancy"] is False
    # Event history should be returned in timestamp order, not arrival order.
    assert [e["event_type"] for e in txn["events"]] == [
        "payment_initiated",
        "payment_processed",
        "settled",
    ]


def test_invalid_event_rejected_with_422(client):
    bad = sample_event()
    del bad["amount"]
    bad["amount"] = -5  # violates ge=0
    resp = client.post("/events", json=bad)
    assert resp.status_code == 422


def test_batch_ingest_isolates_bad_records(client):
    good = sample_event(event_id="b1")
    bad = {"event_type": "payment_initiated"}  # missing required fields
    resp = client.post("/events/batch", json=[good, bad])
    body = resp.json()
    assert body["accepted_count"] == 1
    assert body["rejected_count"] == 1
