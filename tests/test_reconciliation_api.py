from tests.test_events_api import sample_event


def seed_settled_without_processing(client):
    client.post(
        "/events",
        json=sample_event(
            event_id="s1",
            transaction_id="txn-disc-1",
            event_type="settled",
            timestamp="2026-01-05T00:00:00+00:00",
        ),
    )


def test_discrepancy_endpoint_flags_settled_without_processing(client):
    seed_settled_without_processing(client)
    resp = client.get("/reconciliation/discrepancies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    reasons = [row["discrepancy_reason"] for row in body["items"]]
    assert any("settled_without_processing" in r for r in reasons)


def test_summary_group_by_merchant(client):
    client.post("/events", json=sample_event(event_id="e1", transaction_id="t1"))
    resp = client.get("/reconciliation/summary?group_by=merchant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_by"] == "merchant"
    assert any(row["group_key"] == "merchant_1" for row in body["rows"])


def test_transaction_not_found_returns_404(client):
    resp = client.get("/transactions/does-not-exist")
    assert resp.status_code == 404


def test_list_transactions_pagination_and_filters(client):
    for i in range(3):
        client.post(
            "/events",
            json=sample_event(
                event_id=f"e{i}", transaction_id=f"t{i}", merchant_id="merchant_1"
            ),
        )
    resp = client.get("/transactions?merchant_id=merchant_1&page=1&page_size=2")
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["total_pages"] == 2
