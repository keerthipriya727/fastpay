# FastPay Payment Service

A backend service that ingests payment lifecycle events, maintains
transaction state derived from them, and exposes reconciliation reporting
to surface discrepancies between payment and settlement status.

Built with **FastAPI + SQLAlchemy 2.0**. 
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

See `app/models.py` for the full schema.

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
a single mutable-row design.

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

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              #set the variables to run locally
uvicorn app.main:app --reload
```
Set the database url in .env.example
Open `http://localhost:8000/docs` for interactive Swagger UI.

### Load the sample dataset

```bash
python scripts/generate_sample_data.py     # regenerates sample_events.json (optional, one is already included)
python scripts/load_sample_data.py --base-url http://localhost:8000
```

### Optional: running against Postgres

Not required for local development, but if you'd rather point this at a
real Postgres instance you already have running:

```bash
pip install psycopg2-binary alembic
```
Set in `.env`:
```
DATABASE_URL=postgresql+psycopg2://<user>:<password>@localhost:5432/<dbname>
```
Then run migrations instead of relying on `create_all`:
```bash
alembic upgrade head
uvicorn app.main:app --reload
```
If you hit an error about columns/tables not matching what a migration
expects, it almost always means the target database already has
differently-shaped tables in it from a previous attempt. Fastest fix on a
throwaway dev database:
```bash
psql "$DATABASE_URL" -c 'DROP TABLE IF EXISTS events, transactions, merchants, alembic_version CASCADE;'
alembic upgrade head
```

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
`X-API-Key` header, enforced only if `API_KEY` is set in the
environment (empty/unset = auth disabled, for easy local grading).

### Postman collection
`postman/setu_collection.json` — import into Postman; set the `base_url`
collection variable to deployed URL or `http://localhost:8000`.

---

## 4. Deployment (Render)

### Local / manual setup

1. Create and activate a virtual environment.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Set the required environment variables:
   - `DATABASE_URL` — your PostgreSQL connection string
   - `API_KEY` — optional; if set, requests must include `X-API-Key`
4. Start the app:
   `uvicorn app.main:app --host 0.0.0.0 --port 8000`
5. Health endpoint:
   `GET /health`

### Example environment values

- Local PostgreSQL:
  `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/setu_payment_service`
- Auth:
  `API_KEY=my-secret-key`

### Schema setup on first run

The app calls `Base.metadata.create_all()` on startup (see `app/main.py`)
regardless of which database it's pointed at — so the first run creates
all required tables automatically, no separate migration step needed. This
is deliberately kept simple: `create_all()` only creates tables that
don't already exist, so it is safe to restart repeatedly.

If you later evolve the schema and want real, versioned migrations,
use Alembic (`alembic/` is already included). Run:

`alembic upgrade head`

Don't mix `create_all()` and Alembic on the same database unless you are
certain the schema is consistent. If you previously created tables via
`create_all()` and then run Alembic against the same database, you may see
foreign-key/schema mismatch errors. In that case, drop the tables and pick
one approach going forward.

> Example cleanup if needed: `DROP TABLE events, transactions, merchants, alembic_version CASCADE;`

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
- **`create_all` on startup vs. Alembic**: `create_all` is the only
  schema-setup path for local dev (SQLite by default) — no separate
  migration step needed. Alembic (`alembic/`) is included for anyone
  who deploys against a persistent Postgres and wants real, versioned
  migrations instead of `create_all`; it's optional, not required to run
  this service.


## AI tool disclosure

Used Claude (Anthropic) throughout this project — for architecture and schema design decisions, generating the initial FastAPI/SQLAlchemy implementation, the sample data generator, and Render deployment configuration. I reviewed, tested, and debugged the generated code myself, and made the final calls on tradeoffs.