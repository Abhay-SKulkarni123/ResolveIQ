# ResolveIQ

**AI-assisted billing-dispute investigation and resolution agent** — a hiring-assessment build for a
fictional SaaS company, Northstar Cloud.

> A customer writes: *"Our March invoice is wrong. We were charged for 40,000 API calls we never made,
> and our February payment seems to have disappeared."*

ResolveIQ collects the invoice, contract, usage and payment records as evidence, asks an LLM which
explanation fits that evidence, computes every monetary amount with exact decimal arithmetic, cites the
evidence behind each finding, proposes remedies — and then **stops and waits for a human**.

> **Read [`DEPLOYMENT.md`](./DEPLOYMENT.md) before deploying.** This build is a **demo / evaluation**
> deployment, not a production one. Authentication and a real AI provider are the two hard blockers.

---

## 1. Project overview

ResolveIQ is a **reviewer workbench**, not an autonomous agent.

- **The problem.** Billing disputes are slow because the expensive part is not arithmetic — it is
  *reading a complaint and working out which contract clause it might engage*. An analyst opens four
  systems, reconstructs what happened, and re-derives the numbers by hand. That work is repetitive,
  slow, inconsistently done, and mistakes become credits.
- **What ResolveIQ does.** It ingests the source records, snapshots them as immutable fingerprintable
  evidence, recalculates the invoice deterministically under the contract, asks a model which explanation
  fits, cites the evidence behind every finding and computes the monetary impact with exact `Decimal`
  arithmetic — then presents it for a human to accept, reject, request more information about, amend,
  or resolve.
- **Intended audience.** A billing analyst or reviewer (the "reviewer workbench"), and a hiring reviewer
  evaluating the engineering.
- **Key capabilities.** Deterministic recalculating billing engine; evidence collection with content
  fingerprints and staleness detection; closed hypothesis vocabulary; evidence-cited findings; full case
  lifecycle (open → investigate → review → decide → reopen) with an append-only audit trail.
- **Important limitations.** No real authentication (demo headers only); mock-only AI provider; no
  money-moving path (adjustments are deliberately out of scope); container path unverified. See §9 and
  [`DEPLOYMENT.md`](./DEPLOYMENT.md).

### The core idea

> **The model chooses which rule might apply. Deterministic code decides what the rule means in money.**

| Work | Done by | Why not the other way round |
| --- | --- | --- |
| Was the invoice arithmetically correct under the contract? | Pure `Decimal` functions | An LLM doing arithmetic is non-deterministic and confidently wrong |
| Which explanation fits this evidence? | LLM, citing evidence keys | Deterministic code cannot read a complaint and recognise "we cancelled in January" |

The seam is a **typed vocabulary**. The model emits a `hypothesis_code` from a closed set; a registry maps
each code to a pure calculator. The LLM response schema **has no field capable of holding a monetary
amount**, so the separation is enforced by the parser rather than by prompt wording.

---

## 2. Architecture

Hexagonal / ports-and-adapters.

```
                   ┌──────────────────────────────────────────────┐
   browser ──────► │ React workbench (frontend/)                  │
                   └───────────────┬──────────────────────────────┘
                                   │ relative /api/v1 (proxied)
                   ┌───────────────▼──────────────────────────────┐
                   │ FastAPI routes (backend/app/api/)            │
                   │  schemas · deps · errors · middleware        │
                   └───────┬───────────────────────────┬──────────┘
                           │                           │
             ┌─────────────▼──────────────┐   ┌────────▼─────────────┐
             │ services/                  │   │ ports/               │
             │  investigation, evidence   │◄──┤  LlmProvider          │
             │  case lifecycle, recovery  │   │  CaseRepository       │
             └───────┬───────────────┬────┘   └────────┬─────────────┘
        ┌────────────▼──────┐ ┌──────▼──────────┐ ┌─────▼──────────────┐
        │ domain/ +         │ │ adapters/llm/   │ │ adapters/          │
        │ pricing/          │ │  mock provider  │ │  persistence/      │
        │ (pure Decimal)    │ │                 │ │  (SQLAlchemy)      │
        └───────────────────┘ └─────────────────┘ └────────────────────┘
```

**Directories and responsibilities**

| Path | Responsibility |
| --- | --- |
| `backend/app/domain/` | `Money`, evidence, hypotheses, cases. Imports nothing from other layers. |
| `backend/app/pricing/` | Deterministic calculators, rounding, traces, impact registry. |
| `backend/app/ports/` | `LlmProvider`, `CaseRepository`, provider contracts. |
| `backend/app/services/` | Investigation orchestration, evidence collection, citation validation, case lifecycle, recovery. |
| `backend/app/adapters/llm/` | The `MockLlmProvider`. |
| `backend/app/adapters/persistence/` | SQLAlchemy models, repositories, database/session wiring, DDL. |
| `backend/app/api/` | FastAPI routes, Pydantic schemas, dependencies, error handlers, request middleware. |
| `backend/migrations/` | Alembic revisions (`0001`–`0005`). |
| `backend/tests/` | `unit/`, `contract/`, `integration/`. |
| `frontend/src/` | React workbench: `api/`, `components/`, `pages/`, `test/`. |

**One rule is enforced, not merely intended:** `domain/` and `pricing/` import nothing from `adapters/`,
`api/`, SQLAlchemy or FastAPI (checked by `tests/unit/test_layer_boundaries.py`). That is what lets the
money engine be tested with no database and no mocks.

**Request flow — from a dispute to an audit record**

1. `POST /api/v1/disputes` ingests the invoice, contract id, price terms, usage, payments and the
   customer's text. The records are stored as evidence snapshots and the whole ingest payload is kept as
   the case's **source document**.
2. `POST /api/v1/disputes/{id}/investigations` rebuilds the evidence bundle **from what is stored** (never
   from the caller), collects usage summaries and contract-term evidence, runs the deterministic engine,
   then asks the provider to classify and narrate, citing evidence keys.
3. The findings/hypotheses and the engine's figures are persisted on the investigation row, together with
   the evidence fingerprint it read.
4. `POST /api/v1/disputes/{id}/review` records an annotation (accept/reject/request-more-info/amend) on a
   finding, hypothesis or option, refusing if the run is stale.
5. `POST /api/v1/disputes/{id}/decision` closes the case `RESOLVED`/`REJECTED` (reviewer role only), once
   the run has been reviewed.
6. Every step appends to the case's audit trail, readable at `GET /api/v1/disputes/{id}/audit`.

**Deterministic engine vs. provider.** The engine owns every monetary value; the provider only selects a
`hypothesis_code` from the closed set and narrates it, citing evidence. The provider's response schema has
no money field, so a model cannot state an amount even if prompted to.

---

## 3. Technology stack

Versions as actually resolved in this repository.

**Backend**

| Component | Version |
| --- | --- |
| Python | 3.10.9 verified (declared `>=3.10`; the image uses `python:3.12-slim`) |
| FastAPI | 0.142 |
| Uvicorn | 0.54 (`[standard]`) |
| Pydantic / pydantic-settings | 2.13 |
| SQLAlchemy | 2.0.54 |
| Alembic | 1.20 |
| PostgreSQL driver | `psycopg[binary]` 3.2 |
| MySQL driver | PyMySQL 2.2 (pure Python; no `mysqlclient`) |
| pytest | 9.1 |
| mypy | strict, 2.4 |
| Ruff | 0.16 (lint + format) |

**Frontend**

| Component | Version |
| --- | --- |
| React / React DOM | 18.3.1 |
| TypeScript | 5.9 (declared `^5.7`) |
| Vite | 6.4 |
| Tailwind CSS | 3.4 |
| Vitest | 3.2 |
| Testing Library | React 16 |
| ESLint | 9 (flat config) |

**Database**

- **PostgreSQL 16** — the reference target (Compose service, `NUMERIC(19,4)`, JSONB, native `uuid`,
  `timestamptz`).
- **MySQL 8** — supported and **verified** locally (native `JSON`, `CHAR(32)` identifiers, `DATETIME`).
- Both are reached through SQLAlchemy; the driver is chosen by the `DATABASE_URL` scheme.

**Typography.** The UI renders in **Times New Roman** throughout. The single global rule lives on `html`
in `frontend/src/index.css`; the Tailwind `sans` and `mono` tokens both resolve to the same serif stack so
no utility reintroduces a different family, and form controls inherit it.

---

## 4. Prerequisites and local setup

**Required**

- Python 3.10+ and Node 20+
- PostgreSQL 16 or MySQL 8 (SQLite is **not** supported)
- Docker + Docker Compose (optional; for the container path)

**Install**

```bash
cp .env.example .env          # .env is gitignored; never commit it

# backend
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"           # macOS / Linux

# frontend
cd ../frontend
npm install
```

`make` targets exist as a convenience but GNU Make is **not required**; every command in this README is
the raw equivalent.

**Database**

PostgreSQL (reference):

```bash
docker compose up -d db
psql -h 127.0.0.1 -U postgres -f backend/scripts/create_dev_database.sql
```

MySQL 8:

```sql
CREATE DATABASE resolveiq      CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE resolveiq_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'resolveiq'@'%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON resolveiq.*      TO 'resolveiq'@'%';
GRANT ALL PRIVILEGES ON resolveiq_test.* TO 'resolveiq'@'%';
```

Then set `DATABASE_URL` / `TEST_DATABASE_URL`, e.g.
`mysql+pymysql://resolveiq:<password>@localhost:3306/resolveiq`.

**Migrate**

```bash
cd backend
alembic upgrade head        # apply; the schema is at revision 0005
alembic current             # show the revision the database is on
```

**Run**

```bash
# backend
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# frontend (a second terminal)
cd frontend
npm run dev                 # http://localhost:5173
```

**Actual local URLs**

| Service | URL |
| --- | --- |
| Frontend (Vite dev) | `http://localhost:5173` (proxies `/api` → `127.0.0.1:8000`) |
| Backend API | `http://127.0.0.1:8000/api/v1` |
| API docs (Swagger) | `http://127.0.0.1:8000/docs` |
| Health | `http://127.0.0.1:8000/api/v1/health` |
| Frontend (nginx, container) | `http://localhost:8080` |

**Environment configuration.** All variables are read by the **backend only**; the frontend receives none
of them and calls a relative `/api/v1`. Copy `.env.example` to `.env` and edit. Key variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` | `development` \| `test` \| `production` |
| `APP_LOG_LEVEL` | `INFO` | |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated. Never `*` with credentials |
| `DATABASE_URL` | PostgreSQL on `localhost` | SQLAlchemy URL: `postgresql+psycopg://…` or `mysql+pymysql://…` |
| `TEST_DATABASE_URL` | derived sibling | **Disposable** — the migration tests downgrade it to base |
| `CASE_STORE` | `postgres` | Durable store, or `memory` (ephemeral, explicit, never a fallback) |
| `LLM_PROVIDER` | `mock` | The only value implemented |
| `LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | | |
| `LLM_API_KEY` | *(empty)* | Required only once a real provider is wired in |
| `PROMPT_VERSION` | `prompt-v1` | Stored per investigation for attribution |
| `INVESTIGATION_TTL_HOURS` | `24` | Fingerprint validity before a run is stale |
| `INVESTIGATION_MAX_ATTEMPTS` | `3` | Retries before a run completes with its last result |
| `DISPUTE_DESCRIPTION_MAX_CHARS` | `10000` | Untrusted input cap |
| `DEMO_IDENTITY_ENABLED` | `true` | See §8 |
| `DEMO_DEFAULT_ACTOR_ID` / `_ROLE` | `dev-analyst` / `analyst` | |

**Never give a secret a `VITE_` prefix.** Vite inlines `VITE_*` variables into the browser bundle at build
time. There is no `MONEY_SCALE` variable — monetary precision is a schema constant (`NUMERIC(19,4)`).

**Troubleshooting**

| Symptom | Cause | Fix |
| --- | --- | --- |
| `503 DATABASE_UNAVAILABLE` | Database down/unreachable | Start the database; check host/port/credentials in `DATABASE_URL` |
| Startup error mentioning `case_store` | `CASE_STORE` misspelled | Must be exactly `postgres` or `memory` |
| Browser blocked by CORS | Origin not allowed | Add the exact origin to `CORS_ORIGINS` |
| `422 INVALID_USAGE_EVENT` | Naive `datetime` (no timezone) | Send an explicit offset, e.g. `2026-03-15T09:00:00+00:00` |
| `422 INVALID_TIMESTAMP` | Trailing `Z` (invalid on Python 3.10) | Use `+00:00` instead of `Z` |
| Migration test wiped data | `TEST_DATABASE_URL` pointed at real data | It must be a disposable sibling database |
| `pip install` fails building a MySQL driver | Trying `mysqlclient` | Use `pymysql` (already a declared dependency) |

---

## 5. End-to-end usage

The UI is state-routed (no URL router): the left rail switches between **Queue**, **Open dispute**, and a
case detail view. The same steps over the API:

**1. Open a dispute.** `POST /api/v1/disputes` with the invoice, the contract external id, the customer's
`dispute_text`, and (optionally) price terms, usage events, payments and adjustments. Returns `201` with
the case and its evidence fingerprint.

**2. Enter invoice details and usage evidence.** Either in the same `POST /disputes` payload, or later via
`POST /api/v1/disputes/{id}/evidence`. Attaching new records changes the fingerprint, which marks any
investigation that did not read it **stale** and re-opens a closed/awaiting-review case.

**3. Supply contract price terms.** `price_terms[]` each name a `metric_key`, a `billing_mode`
(`PER_UNIT` | `TIERED` | `COMMITMENT`), the `currency`, `included_units`, and the mode's prices
(`unit_price` / `overage_price` / `minimum_commitment` / `tiers`).

**4. Run an investigation.** `POST /api/v1/disputes/{id}/investigations`. Returns `201` with a new
investigation version (never a repeat — a case that already has runs gets `n+1`, and earlier runs keep
their evidence). The mock provider is in-process and deterministic, so the run completes before the
response.

**5. Interpret calculated amounts and findings.** `GET /api/v1/disputes/{id}` returns every investigation
with its `calculation` (`recalculated_total`, `recorded_total`, `difference`, `outstanding`,
`unresolved_metrics`, `is_provisional`), per-line traces (`rule_ref`, `calculated_amount`,
`recorded_amount`), `findings`, `hypotheses` (with a computed `impact`) and `resolution_options`.

**6. Inspect evidence citations.** Each finding's `supporting_evidence` is a list of human-readable
evidence keys (e.g. `invoice:INV-2026-03-0042#line:LI-0007`), and the run carries the evidence it read.

**7. Review a finding.** `POST /api/v1/disputes/{id}/review` with `target_type`
(`FINDING`|`HYPOTHESIS`|`RESOLUTION_OPTION`), `target_id`, `action`
(`ACCEPT`|`REJECT`|`REQUEST_MORE_INFO`|`AMEND`) and a `rationale`. Refused with `409` if the run is stale.

**8. Resolve or reject a case.** `POST /api/v1/disputes/{id}/decision` with
`{"decision": "RESOLVED" | "REJECTED", "rationale": "…"}`. **Reviewer role only** (`403` otherwise), and
refused with `409` unless the current run has at least one review. It moves no money.

**9. Reopen a case.** `POST /api/v1/disputes/{id}/reopen` with a mandatory `reason`, moving a
`RESOLVED`/`REJECTED` case to `REOPENED`. It starts no investigation — re-running is a separate call.

**10. Inspect investigation history and the audit trail.** `GET /api/v1/disputes/{id}/audit` returns the
append-only trail oldest-first; every investigation version is retained on the case detail.

### Worked synthetic example

> This is **synthetic test data**, not a real customer invoice.

| Field | Value |
| --- | --- |
| Recorded invoice amount | **USD 28.40** |
| Usage | **41,234** API calls in March 2026 |
| Included units | **10,000** |
| Unit price | **USD 0.0013** |
| Contract term | `PER_UNIT`, USD, `included_units=10000`, `unit_price=0.0013` on metric `api_calls` |

Deterministic recalculation: `billable = max(41234 − 10000, 0) = 31234`;
`31234 × 0.0013 = 40.6042` → rounded once at the line boundary → **USD 40.60**.
The **difference magnitude is USD 12.20**.

Observed live on this build (mock provider, PostgreSQL store): `recalculated_total = 40.6000`,
`recorded_total = 28.4000`, `difference = 12.2000`, `unresolved_metrics = []`, `is_provisional = false`,
resulting in `COMPLETE`; a reviewer `ACCEPT` followed by a `RESOLVED` decision closed the case.

Submit it over the API with a `+00:00` timestamp for the usage event:

```json
{
  "dispute_external_id": "DSP-DEMO-0001",
  "contract_external_id": "CTR-DEMO-0001",
  "dispute_text": "Charged for calls beyond our included allowance at the wrong rate.",
  "invoice": {
    "external_id": "INV-DEMO-0001", "currency": "USD",
    "period_start": "2026-03-01", "period_end": "2026-03-31",
    "stated_total": {"amount": "28.40", "currency": "USD"},
    "lines": [{"metric_key": "api_calls", "line_type": "USAGE",
               "recorded_amount": {"amount": "28.40", "currency": "USD"}}]
  },
  "price_terms": [{"metric_key": "api_calls", "billing_mode": "PER_UNIT", "currency": "USD",
                   "included_units": "10000", "unit_price": {"amount": "0.0013", "currency": "USD"}}],
  "usage_events": [{"external_id": "UE-DEMO-1", "dedupe_key": "UE-DEMO-1", "metric_key": "api_calls",
                    "occurred_at": "2026-03-15T09:00:00+00:00", "quantity": "41234", "unit": "calls"}]
}
```

---

## 6. API and data model

**Routes** (all under `/api/v1`; request bodies reject unknown fields):

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness; reports provider *mode* only, never key material. Does not touch the DB. |
| `GET` | `/capabilities` | Provider mode, model, prompt version, store kind, feature flags, identity mode. |
| `POST` | `/disputes` | Open a dispute case |
| `GET` | `/disputes` | List cases (`status`, `limit`, `offset`), newest first |
| `GET` | `/disputes/{dispute_id}` | Detail: staleness, evidence, every investigation, reviews, decisions |
| `POST` | `/disputes/{dispute_id}/evidence` | Attach records/snapshots (idempotent; changes fingerprint) |
| `POST` | `/disputes/{dispute_id}/investigations` | Run an investigation; `201`, new version |
| `POST` | `/disputes/{dispute_id}/review` | Record a reviewer annotation; `201` |
| `POST` | `/disputes/{dispute_id}/decision` | Close the case `RESOLVED`/`REJECTED`; reviewer only; `201` |
| `POST` | `/disputes/{dispute_id}/reopen` | Reopen a closed case; mandatory reason |
| `GET` | `/disputes/{dispute_id}/audit` | Append-only audit trail, oldest-first |

`/docs` (Swagger) and `/openapi.json` are always served. A local snapshot of the OpenAPI document is kept
at `frontend/src/api/openapi.snapshot.json` and checked against the TypeScript types by
`frontend/src/test/contract.test.ts`.

**Request schemas and validation** (`backend/app/api/schemas.py`):

- `OpenDisputeRequest`: `invoice`, `contract_external_id`, `dispute_text`, `severity`
  (`LOW`|`MEDIUM`|`HIGH`|`CRITICAL`), and optional `price_terms`, `usage_events`, `payments`,
  `adjustments`.
- `InvoiceInput`: `currency` must match `^[A-Z]{3}$` (ISO 4217), `period_start`/`period_end`, a
  `stated_total`, and at least one `lines[]` entry.
- `PriceTermInput`: `billing_mode` ∈ `PER_UNIT`|`TIERED`|`COMMITMENT`; `included_units` decimal string;
  mode-specific prices; optional `tiers`.
- `UsageEventInput`: `quantity` matches `^-?\d+(\.\d+)?$`; `occurred_at` is a timestamp string.
- `ReviewRequest` / `DecisionRequest`: as above; `DecisionRequest.rationale` is mandatory.
- Amounts are **strings** everywhere (`^-?\d+(\.\d+)?$`) so JSON floating point never touches money.

**Evidence.** Eight evidence types: `INVOICE`, `INVOICE_LINE`, `USAGE_SUMMARY`, `USAGE_EVENT`, `PAYMENT`,
`PAYMENT_ALLOCATION`, `CONTRACT_TERM`, `DISPUTE_TEXT`. Evidence is stored as an immutable snapshot with a
content fingerprint; citations use human-readable keys (`invoice:INV-2026-03-0042#line:LI-0007`), never
surrogate ids, and are validated against an allowlist with hard failure.

**Contract terms and usage summaries.** A contract price term becomes evidence under the key
`contract:{contract_external_id}#term:{metric_key}`, and a usage summary under
`usage:{metric}:{period_start}..{period_end}`. The recalculation finding cites the invoice, the invoice
line, and the contract term it applied.

**Investigations, reviews, decisions, audit.** An investigation is never mutated: re-running adds a new
version, and earlier runs retain the evidence they read. A review targets one finding/hypothesis/option
and may store an `AMEND`ed narrative without overwriting the original. A decision is a separate record
that closes the lifecycle and carries no amount. The audit trail is append-only and never edited.

**Timezone handling (explicit, not assumed).** Timestamps must carry an offset. A naive
`datetime`/timestamp is **refused** (`422 INVALID_USAGE_EVENT`) because it cannot be placed unambiguously
in a period; a trailing `Z` is rejected on Python 3.10 (`422 INVALID_TIMESTAMP`). The database session is
pinned to UTC and reads back UTC. Send `2026-03-15T09:00:00+00:00`. The frontend converts
`datetime-local` input to an explicit `+00:00` instant before submitting, and the field is labelled
"Entered and stored in UTC."

---

## 7. Testing and quality checks

Measured on Windows / Python 3.10.9 against a local **MySQL 8** server (74 integration tests) and the
frontend toolchain. Re-run to reproduce. Report only what these commands actually printed.

| Suite | Command | Result |
| --- | --- | --- |
| Backend unit + contract | `cd backend && .venv/Scripts/python.exe -m pytest tests/unit tests/contract` | **1058 passed, 3 skipped** |
| Backend integration | `.venv/Scripts/python.exe -m pytest tests/integration` | **74 passed** (0 failed, 0 skipped) |
| Ruff | `.venv/Scripts/python.exe -m ruff check .` | **All checks passed** |
| mypy (strict) | `.venv/Scripts/python.exe -m mypy app` | **Success, 52 source files** |
| Migration parity | `alembic current` / `alembic heads` | **`0005_audit_events`** on both |
| Frontend tests | `cd frontend && npm test` | **66 passed (4 files)** |
| TypeScript | `npm run typecheck` | **clean** |
| ESLint | `npm run lint` | **clean** |
| Frontend build | `npm run build` | **clean** — `212.09 kB` JS (`63.88 kB` gzip), `25.14 kB` CSS |
| Compose config | `docker compose config` | **valid** (build/up **not** run — no Docker daemon) |

**Coverage by kind**

- **Unit** — domain/pricing/engine, services, API surface against the in-memory store, persistence model
  metadata. The HTTP tests deliberately run against `CASE_STORE=memory`, so they double as the "no
  database required" path.
- **Contract** — every `LlmProvider` must satisfy a conformance suite; the mock provider is the only one.
- **Integration** — real-database behaviour: migrations up/down, model/migration parity, CHECK-constraint
  behaviour, dialect portability. Requires a live database.
- **Frontend** — component/interaction tests (Testing Library) and an OpenAPI-snapshot contract test that
  checks the TypeScript types against the backend schema.

**End-to-end** is not an automated suite. It was exercised manually — a synthetic dispute open →
investigate → `ACCEPT` → `RESOLVED` against a live server — and against the running app's routes and
overlays, including a browser-engine check that confirmed every representative element (headings, body,
labels, inputs, textarea, select, buttons, table cells, and the `font-mono`/`amount` spans) computes to
`"Times New Roman", Times, serif`.

Not run / unverified in this environment: Docker image builds and `docker compose up` (no daemon).
A one-off observation: a read issued in the same instant as a write response briefly reflected the
pre-write state before the request-edge commit finalized; not reproduced on retry, and the UI re-fetches
after writes.

---

## 8. Security and limitations

**In place.** Queries are built with SQLAlchemy constructs and bound parameters — no user input is
interpolated into SQL. Dispute descriptions are untrusted input, capped and treated as data. The model
has no capability to act: it cannot approve, adjust, or call a tool, and its response schema has no money
field. Payments hold only an `external_id`, an amount and its per-invoice allocations — no card or bank
data. `.env` and `.env.*` are gitignored (`.env.example` holds placeholders only); no `VITE_*` secret
exists. `/health` reports the provider *mode*, never key material.

**Not real — do not mistake this for authentication.** Identity is a pair of development headers
(`X-Actor-Id`, `X-Actor-Role`). **These are trivially spoofable.** The only role gate is the decision
endpoint requiring `reviewer`, and it trusts this header. `DEMO_IDENTITY_ENABLED=false` does not replace
it with real authentication — it only stops the development default from being applied. A real deployment
needs signed tokens/sessions, a server-verified principal, server-side authorization, and enforced
separation of duties.

**The AI provider is mock-only.** `LLM_PROVIDER` is `Literal["mock"]`; `MockLlmProvider` is deterministic
and offline, and is the only provider implemented. The port is exercised by the contract suite, but
nothing here has been run against a hosted model. Monetary figures always come from the deterministic
engine.

**Which databases are actually tested.** PostgreSQL is the reference design target; MySQL 8 was verified
locally (unit, contract and 74 integration tests, and the end-to-end smoke). The honest caveat: MySQL
cannot store a timezone offset (`DATETIME`), so the naive-vs-aware timestamp distinction is enforced by
the API and the billing domain, and would surface differently on PostgreSQL's `timestamptz`. Do not claim
PostgreSQL compatibility merely because a Compose service exists — run the suite against it first.

**Other limitations.** Adjustments (any money-moving path) are deliberately not implemented. There is no
real LLM provider and no production authentication. No rate limiting, no `TrustedHostMiddleware`, and
`/docs` is always exposed. There is no deterministic demo seed data — a fresh database is empty. The
Docker/Compose path is unverified.

**Residual risk.** Prompt injection is mitigated, not eliminated: a model can still be led to a *wrong but
well-formed, correctly-cited* conclusion. Mandatory human review plus the deterministic engine exist for
exactly that reason.

---

## 9. Deployment

> **Verdict: demo / evaluation deployment ready — not production ready.** Full runbook, environment
> matrix, health/log guidance and the production blocker list are in **[`DEPLOYMENT.md`](./DEPLOYMENT.md)**.

Two hard blockers keep this out of production: **no real authentication** and **mock-only AI provider**.
See `DEPLOYMENT.md` §Production blockers for the complete list (trusted hosts, docs exposure, TLS,
reverse-proxy expectations, and the unverified container path).

**Images.** `backend/Dockerfile` is Python 3.12-slim, runs as a non-root user, and includes `alembic.ini`
and `migrations/` so migrations can run in the container; it declares a healthcheck. `frontend/Dockerfile`
is a two-stage build (`npm ci` → `npm run build` → nginx) and `frontend/nginx.conf` forwards `/api` to the
backend and serves the SPA, matching what Vite's dev proxy does.

**Demo (containers).**

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build db
docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm api alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d api web-prod   # frontend on :8080
```

The demo overlay runs the API from the **image** (no bind mount, no `--reload`), serves the built bundle
via nginx, and does **not** publish the database port to the host. The plain `docker-compose.yml` is the
**local development** stack (hot reload, Vite dev server).

> **The container path has never been executed in this repository** — no Docker daemon was available
> either time it was touched. `docker compose config` (and the merged overlay) parses cleanly, but treat
> every container command as **unverified** until you run it once and watch it come up.

Migrations are a **separate release step** run before the new version serves traffic — never on start, and
never a `downgrade base` against anything but the disposable test database.

---

## 10. Documentation map

| Document | Contents |
| --- | --- |
| `README.md` | This guide: overview, architecture, setup, usage, API, testing, security. |
| `DEPLOYMENT.md` | Deployment runbook, environment matrix, health/logs, TLS, production blockers. |
| `backend/.env.example` | Every backend setting, commented, with placeholders. |
| `frontend/src/api/openapi.snapshot.json` | Snapshot of the API schema, checked against the TS types. |

`docs/` (engineering decisions, system design, traceability) is intentionally **local-only** and
gitignored; it is not published to GitHub.

---

## 11. Assessment scope

Built to demonstrate judgement under a billing-correctness constraint: exact decimal arithmetic, a hard
seam between interpretation and computation, evidence a human can check, and an AI that cannot commit.
Where a requirement was deliberately not built (adjustments, real providers), that is a decision recorded
with its reasoning rather than an omission.

Nothing in this README is marked verified unless the command above was run and its result recorded. No
requirement is marked "implemented and tested" on the basis of a skipped or failing test.
