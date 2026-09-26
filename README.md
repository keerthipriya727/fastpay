
# Setu Payment Reconciliation Service

A backend service that ingests payment lifecycle events, maintains
transaction state derived from them, and exposes reconciliation reporting
to surface discrepancies between payment and settlement status.

Built with **FastAPI + SQLAlchemy 2.0 + PostgreSQL** (SQLite supported for
zero-setup local runs). Migrations via **Alembic**.

---

## 1. Architecture overview

### Data model

Three tables, event-sourced:

- **`merchants`** — reference table, upserted lazily from event data.
- **`events`** — append-only, immutable. One row per `event_id`
  (unique-constrained — this is the idempotency guard, enforced by the
  database, not just application code). This is the source of truth.
- **`transactions`** — derived/materialized current state per
  `transaction_id`, rebuilt from that transaction's full event history
  every time a new event for it is ingested. Read endpoints query this
  table, not `events`, so reads stay cheap regardless of how many events a
  transaction has accumulated.

See `app/models.py` for the full schema, or `alembic/versions/0001_initial.py`
for the SQL DDL.

### Why event-sourced instead of a single mutable table

Payments are inherently a sequence of state changes with real audit
requirements, and the assignment explicitly asks for duplicate-event and
discrepancy handling — both of which require knowing the *history*, not
just the current state. Storing raw events and deriving transaction state
from them gives you:
- a natural idempotency key (`event_id`)
- the ability to replay/recompute state if the derivation logic changes
- an audit trail for free
- a clean place to detect out-of-order and conflicting events

The tradeoff is one extra write (or state recompute) per event compared to
a single mutable-row design. At this data volume that's irrelevant.

### State machine (the core logic)

Valid sequences: `payment_initiated → payment_processed → settled` (happy
path) or `payment_initiated → payment_failed`. Events are applied in
**event-timestamp order**, not arrival order, so out-of-order delivery is
handled correctly. This lives in `app/services/state_machine.py` as a
pure, DB-free function — see `tests/test_state_machine.py` for the exact
cases it's tested against.

Two categories of discrepancy, computed differently:

| Type | Example | When computed |
|---|---|---|
| **Structural** | `settled` with no prior `processed`; `settled` after `failed`; conflicting amount across events for the same transaction | At ingest time, stored on the transaction row (`has_discrepancy`, `discrepancy_reason`) |
| **Time-based (SLA)** | `processed` but never `settled` within `UNSETTLED_THRESHOLD_HOURS` (default 24h) | At query time in `GET /reconciliation/discrepancies`, against a reference clock — it can't be precomputed, since it becomes true purely by *time passing with no new event* |

`GET /reconciliation/discrepancies` returns the union of both.

### Idempotency

`event_id` is unique-constrained at the DB level. `POST /events` checks
for an existing row first (fast path), but correctness under concurrent
duplicate requests comes from catching the constraint violation on
insert, not the pre-check — see `app/services/ingestion.py`.

### Processing model: synchronous, not queued

Ingestion validates, writes the event, and recomputes transaction state
in one request / one DB transaction. No message queue or background
worker. This is a deliberate scope decision for ~10k events and a
small-service context — see [Tradeoffs](#5-assumptions--tradeoffs) for
the scale-up path.

### Pagination

`GET /transactions` and `GET /reconciliation/discrepancies` use
offset/limit pagination (`page`, `page_size`), not cursor-based. Simpler
to test manually (e.g. in Postman) and sufficient at this scale; documented
tradeoff below.

---

## 2. Setup — local development

### Option A: zero setup (SQLite)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # default DATABASE_URL is sqlite
uvicorn app.main:app --reload
```

The app creates its own tables on startup in this mode (`Base.metadata.create_all`
— fine for a quick look, not what you'd rely on for real migrations).
Open `http://localhost:8000/docs` for interactive Swagger UI.

### Option B: Docker Compose (Postgres — matches production target)

```bash
docker compose up --build
```

This starts Postgres, waits for it to be healthy, runs `alembic upgrade head`,
then starts the API on `http://localhost:8000`.

### Load the sample dataset

```bash
python scripts/generate_sample_data.py     # regenerates sample_events.json (optional, one is already included)
python scripts/load_sample_data.py --base-url http://localhost:8000
```

### Run tests

```bash
pytest
```

Tests run against an isolated in-memory SQLite DB per test (see
`tests/conftest.py`) — fast, but a documented simplification versus
testing against real Postgres (see Tradeoffs).

---

## 3. API documentation

Full interactive docs at `/docs` (Swagger) or `/redoc` once running. Summary:

### `POST /events`
Ingest a single event.
```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```
→ `201`, `{"event_id", "status": "accepted"|"duplicate_ignored", "transaction_id", "transaction_payment_status"}`.
Resubmitting the same `event_id` is a no-op, not an error.

### `POST /events/batch`
Same shape, but body is a JSON array of events (used to load `sample_events.json`).
Bad records in the batch are reported back individually; they don't fail the whole batch.

### `GET /transactions`
Query params: `merchant_id`, `status` (`initiated|processed|failed|unknown`),
`date_from`, `date_to`, `page`, `page_size`, `sort_by`, `sort_order`.
Returns a paginated list of transaction summaries.

### `GET /transactions/{transaction_id}`
Full detail: current status, merchant info, and complete event history in
timestamp order. `404` if unknown.

### `GET /reconciliation/summary?group_by=merchant|date|status`
Aggregated counts/amounts (total, settled, unsettled, discrepant) grouped
by the given dimension.

### `GET /reconciliation/discrepancies`
Paginated list of transactions with a payment/settlement inconsistency,
each with its `discrepancy_reason`.

### Auth
Optional `X-API-Key` header, enforced only if `API_KEY` is set in the
environment (empty/unset = auth disabled, for easy local grading).

### Postman collection
`postman/setu_collection.json` — import into Postman; set the `base_url`
collection variable to your deployed URL or `http://localhost:8000`.

---

## 4. Deployment

**What's included:** a production-shaped `Dockerfile` (multi-stage,
non-root user, healthcheck) and `docker-compose.yml` for local Postgres.

**What you still need to do:** actually deploy it. Pick one:
- **Render / Railway / Fly.io** — point them at this repo's Dockerfile,
  attach a managed Postgres, set `DATABASE_URL` and (optionally) `API_KEY`
  as env vars, run `alembic upgrade head` as a release-phase command before
  the app starts.
- **AWS (ECS/Fargate) / GCP Cloud Run** — same idea, more setup.

Whichever you choose: run migrations as a **separate release step**, not
inside the app's own `on_event("startup")` — the current
`docker-compose.yml` command chains them (`alembic upgrade head && uvicorn ...`)
for local simplicity, which is fine for a single instance but would race
if you ever scaled to multiple app instances starting concurrently.

`GET /health` is the endpoint to point your platform's health check at.

> **Deployed URL:** _fill in after you deploy — e.g. `https://setu-payments.onrender.com`_

---

## 5. Assumptions & tradeoffs

- **"Reconciliation" = payment-vs-settlement state consistency**, derived
  entirely from the ingested event stream — there's no separate external
  bank/PSP statement to reconcile against in this scope, since none was
  provided. If one exists, `reconciliation` would instead compare
  `transactions` against that external feed; the discrepancy detection
  approach (structural + time-based) would extend naturally.
- **Synchronous ingestion, no queue.** Chosen for simplicity at this scale.
  At real production volume, the upgrade path is: `POST /events` writes to
  the append-only `events` table (or a queue) fast and returns immediately;
  a worker recomputes `transactions` asynchronously. The event-sourced
  design here makes that split straightforward later — it doesn't require
  a schema change, just moving `_recompute_transaction_state` out of the
  request path.
- **Offset pagination, not cursor-based.** Simpler to demo/test; would
  switch to cursor-based (keyset on `last_event_at, transaction_id`) if
  writes were concurrent enough for offset pages to skip/duplicate rows.
- **SLA threshold (24h) and reference clock are configurable**, not
  hardcoded — because "how long is too long before it's a discrepancy" is
  a business decision. `RECONCILIATION_REFERENCE_TIME` can be pinned to
  get reproducible results against the static sample dataset instead of
  "now" drifting relative to it.
- **Last-write-wins** for merchant name and for amount/currency when
  events disagree (the conflict itself is *also* flagged as a
  discrepancy, so it's visible, not silently swallowed).
- **`create_all` on startup vs. Alembic**: `create_all` is only there so
  Option A (SQLite, zero setup) works with no extra step. Docker Compose
  and any real deployment should run `alembic upgrade head` instead —
  that's the actual migration path (`alembic/`).
- **Tests use in-memory SQLite, not Postgres**, for speed and zero
  external dependency in CI. This is a real tradeoff: SQLite's JSON/type
  handling and concurrency behavior differ slightly from Postgres. A
  stronger setup would run the integration tests against a real Postgres
  container (e.g. via `testcontainers`); noted here rather than silently
  glossed over.

## AI tool disclosure

_Fill in per the assignment's submission requirements — be specific about
what was AI-assisted vs. hand-written/reviewed._
