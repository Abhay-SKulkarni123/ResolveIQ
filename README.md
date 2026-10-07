# ResolveIQ

**AI-assisted billing-dispute investigation and resolution agent** — a hiring-assessment build for a
fictional SaaS company, Northstar Cloud.

> A customer writes: *"Our March invoice is wrong. We were charged for 40,000 API calls we never made,
> and our February payment seems to have disappeared."*

ResolveIQ collects the invoice, contract, usage and payment records as evidence, asks an LLM which
explanation fits that evidence, computes every monetary amount with exact decimal arithmetic, cites the
evidence behind each finding, proposes remedies — and then **stops and waits for a human**.

---

## 1. Project overview

The product is a **reviewer workbench**, not an autonomous agent. The system produces a reasoned,
evidence-cited investigation; a human decides what happens next. There is deliberately no code path by
which the system moves money.

## 2. The problem

Billing disputes are slow because the expensive part is not arithmetic — it is *reading a complaint and
working out which contract clause it might engage*. An analyst opens four systems, reconstructs what
happened, and re-derives the numbers by hand. That work is repetitive, slow, and inconsistently done,
and mistakes are expensive because they become credits.

## 3. The core idea

> **The model chooses which rule might apply. Deterministic code decides what the rule means in money.**

| Work | Done by | Why not the other way round |
| --- | --- | --- |
| Was the invoice arithmetically correct under the contract? | Pure `Decimal` functions | An LLM doing arithmetic is non-deterministic and confidently wrong |
| Which explanation fits this evidence? | LLM, citing evidence keys | Deterministic code cannot read a complaint and recognise "we cancelled in January" |

The seam is a **typed vocabulary**. The model emits a `hypothesis_code` from a closed set of 10; a registry
maps each code to a pure calculator. The LLM response schema **has no field capable of holding a monetary
amount**, so the separation is enforced by the parser rather than by prompt wording (ADR-003/004).

## 4. Core workflow

```
ingest → evidence snapshot → deterministic recalculation → LLM interpretation
      → citation validation → impact computation → findings + hypotheses
      → HUMAN REVIEW → (optional) reopen
```

1. **Ingest** a dispute with the customer's description.
2. **Snapshot** the invoice, contract, usage and payment records as immutable, fingerprinted evidence.
3. **Recalculate** the invoice deterministically under the contract terms, producing an ordered
   `calculation_trace` naming the clause applied at every line.
4. **Interpret** with the LLM, which sees only the evidence bundle and must cite keys from it.
5. **Validate** the response against the schema, then against a citation allowlist. A response citing a
   non-existent key is rejected in full.
6. **Compute impact** from the hypothesis code via the registry — the model never states an amount.
7. **Review** by a human: accept, reject, request more information, or amend.

## 5. Architecture

Hexagonal / ports-and-adapters (ADR-002):

```
domain/     Money, evidence, hypotheses — imports nothing from any other layer
pricing/    deterministic calculators, rounding, traces, impact registry
ports/      LlmProvider, CaseStore, Interpretation protocols
services/   investigation, evidence collection, citation validation
adapters/   SQLAlchemy persistence, mock LLM provider
api/        FastAPI routes and schemas
```

**The layer rule is enforced by an import check**: `domain/` and `pricing/` import nothing from `adapters/`,
`api/`, SQLAlchemy, or FastAPI. That is what lets the money engine be tested with no database and no mocks.

## 6. Technology stack

- **Backend** Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, `uvicorn`
- **Database** PostgreSQL 16 (reference) or MySQL 8.0
- **Frontend** React 19, TypeScript, Vite, Tailwind, Vitest, Testing Library
- **Tooling** Ruff (lint/format), mypy `--strict`

## 7. Deterministic billing

`Money` is a dedicated value object over `Decimal`. Constructing one from a `float` raises
`UnsupportedAmountTypeError` — binary floating point cannot represent `0.0007` exactly, and this codebase
treats sub-cent unit prices as in scope (ADR-001).

- Every monetary column is `NUMERIC(19,4)`. A test asserts `float` never appears in the `pricing/` path.
- Rounding is explicit and owned by `pricing/rounding.py`: `HALF_UP` at invoice boundaries, `HALF_EVEN`
  for splits. There is exactly one rounding per line boundary, not one per operation.
- Three billing modes are implemented: `PER_UNIT`, `TIERED`, `COMMITMENT`.
- Recalculating the §6.4 worked example returns **21.86** against a recorded **28.40**.

## 8. AI investigation approach

The default provider is `MockLlmProvider`: deterministic, offline, and the only provider implemented. A real
provider is **not built** (ADR-026, OQ-08) — the port exists and is exercised by 32 contract tests, but
nothing here has been run against a hosted model.

The response schema is a Pydantic model that rejects unknown fields and has no money field. The model's job
is narrowed to classification and narration; it selects from the closed `hypothesis_code` set and cites
evidence. If it returns something else, the response is rejected.

## 9. Evidence and citations

Evidence is stored as **immutable snapshots with a content fingerprint** (ADR-006). Re-adding identical
evidence is a no-op; changing any byte changes the fingerprint, which is what staleness detection keys on.

Citations use human-readable keys (`invoice:INV-2026-03-0042#line:LI-0007`), never surrogate IDs, so a
reviewer can find the cited line without the tool. Validation is against an allowlist with **hard
failure** (ADR-005) — a partially-valid response is discarded, not trimmed.

## 10. Human review workflow

Four review verbs: `ACCEPT`, `REJECT`, `REQUEST_MORE_INFO`, `AMEND`. Plus `REOPEN`, which supersedes a
resolved case while retaining prior investigations.

- Analyst/reviewer **separation of duties** is a real design decision; its *enforcement* is a development
  stub (see §Security notes).
- Reopening creates a new investigation rather than mutating the old one.
- **Adjustments are not implemented, by design.** There is no money-moving path in this system, so there is
  no duplicate-adjustment risk to mitigate yet.

## 11. Data persistence

13 tables, 44 CHECK constraints, two Alembic revisions. Money is `NUMERIC(19,4)`/`DECIMAL(19,4)`; JSON
snapshots; foreign keys with `RESTRICT` on referenced parents; a named unique index as the arbiter for
idempotent evidence inserts.

The dispute/investigation FK cycle is broken by creating `disputes` without `current_investigation_id` and
adding that constraint with `ALTER TABLE` after `investigations` exists.

## 12. Local setup

```bash
cp .env.example .env          # .env is gitignored; never commit it
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"    # Windows
cd ../frontend && npm install
```

`make` targets exist as a convenience but GNU Make is **not required**; every command below is the raw
equivalent.

## 13. Environment variables

Read by the backend only — the frontend receives none of them.

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` | `development` \| `test` \| `production` |
| `APP_LOG_LEVEL` | `INFO` | |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated. Not `*` with credentials |
| `DATABASE_URL` | PostgreSQL | SQLAlchemy URL, e.g. `postgresql+psycopg://…` or `mysql+pymysql://…` |
| `TEST_DATABASE_URL` | derived | Sibling database. **Disposable** — the migration tests downgrade it to base |
| `LLM_PROVIDER` | `mock` | Only `mock` is implemented |
| `LLM_API_KEY` | *(empty)* | Required only once a real provider is wired in |
| `LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | | |
| `PROMPT_VERSION` | `prompt-v1` | Stored per investigation for attribution |
| `INVESTIGATION_TTL_HOURS` | `24` | Fingerprint validity before a run is stale |
| `DISPUTE_DESCRIPTION_MAX_CHARS` | `10000` | Untrusted input cap |
| `MONEY_SCALE` | `4` | Matches `NUMERIC(19,4)` |
| `DEMO_IDENTITY_ENABLED` | `true` | See §Security notes |
| `DEMO_DEFAULT_ACTOR_ID` / `_ROLE` | `dev-analyst` / `analyst` | |

**Never give a secret a `VITE_` prefix.** Vite inlines `VITE_*` variables into the browser bundle at build
time. The frontend client uses a relative `/api/v1` and needs no API host setting at all.

## 14. Database setup

**PostgreSQL (reference):**

```bash
docker compose up -d db
psql -h 127.0.0.1 -U postgres -f backend/scripts/create_dev_database.sql
```

**MySQL 8:**

```sql
CREATE DATABASE resolveiq      CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE resolveiq_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'resolveiq'@'%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON resolveiq.*      TO 'resolveiq'@'%';
GRANT ALL PRIVILEGES ON resolveiq_test.* TO 'resolveiq'@'%';
```

Then point `DATABASE_URL` / `TEST_DATABASE_URL` at them, e.g.
`mysql+pymysql://resolveiq:<password>@localhost:3306/resolveiq`. The PyMySQL driver is a declared
dependency; `mysqlclient` is not needed because PyMySQL is pure Python.

## 15. Migrations

```bash
cd backend
alembic upgrade head            # apply
alembic downgrade base          # drop everything — test database only
alembic revision --autogenerate -m "description"
```

Run migrations as a **separate step before** a new version serves traffic, not on application start.

## 16. Running the backend

```bash
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/api/v1/health
```

API: `GET /health`, `GET /capabilities`, `GET /disputes`, `POST /disputes`,
`GET /disputes/{id}`, `POST /disputes/{id}/evidence`, `POST /disputes/{id}/investigations`,
`POST /disputes/{id}/review`, `POST /disputes/{id}/reopen`.

## 17. Running the frontend

```bash
cd frontend
npm run dev        # http://localhost:5173 — proxies /api to 127.0.0.1:8000
npm run build      # tsc --noEmit && vite build
```

Start the backend first. The queue and detail screens read live API responses; there is no fixture mode,
because a workbench rendering invented cases is indistinguishable from one rendering real ones.

## 18. Testing

Measured on Windows / Python 3.10.9. Re-run to reproduce.

| Suite | Command | Result |
| --- | --- | --- |
| Backend unit | `cd backend && pytest tests/unit` | **831 passed**, 0 failed |
| Backend integration | `pytest tests/integration` | **42 passed, 20 failed** of 62 — see §20 |
| Ruff | `ruff check app tests` | clean |
| mypy (strict) | `mypy app` | clean, 47 source files |
| Frontend | `cd frontend && npm test` | **53 passed** |
| TypeScript | `npm run typecheck` | clean |
| ESLint | `npm run lint` | clean |
| Frontend build | `npm run build` | clean, 168 kB JS / 53 kB gzipped |

**The 20 integration failures are real and are not dismissed.** They are PostgreSQL-catalog and
dialect-expectation assumptions in the test harness, not confirmed business-logic defects — but they are
also not yet proven to be harmless. The suite is **not** green.

## 19. Deployment

Backend image (`backend/Dockerfile`) is Python 3.12-slim, non-root, with a healthcheck; it includes
`alembic.ini` and `migrations/` so migrations can run in the container.

Frontend (`frontend/Dockerfile`) is a two-stage build: `npm ci` → `npm run build` → nginx serving
`dist/`. `frontend/nginx.conf` forwards `/api` to the backend, matching what Vite's dev proxy does, so
routing is identical in development and production.

```bash
docker compose run --rm api alembic upgrade head   # 1. migrate
docker compose up -d db api web-prod                # 2. serve (frontend on :8080)
```

`docker compose up` has **never been executed** — no Docker daemon was available in the build
environment. The Compose file parses (`docker compose config` lists `db`, `api`, `web`, `web-prod`),
but treat the container path as unverified.

## 20. Known limitations

**PostgreSQL was the original target.** MySQL support was added because the assessment environment provided
MySQL. Verified consequences of running there:

| Concern | PostgreSQL | MySQL |
| --- | --- | --- |
| JSON snapshots | `JSONB` (indexable, comparable) | native `JSON` — **no JSONB equivalent** |
| Identifiers | native `uuid` | `CHAR(32)` |
| Timestamps | `timestamptz` — offset is stored | `DATETIME` — **no timezone stored** |

The timestamp row is a genuine semantic difference, not a cosmetic one: MySQL cannot record an offset, so
a naive-versus-aware `datetime` defect would not surface there the way it would on PostgreSQL.

The model and migration CHECK constraints are portable across both. `btrim` became `TRIM`; the PostgreSQL
regex operator (`~`) has no MySQL equivalent, so the ISO-4217 and SHA-256 checks were rewritten as
collation-independent `ASCII`/`SUBSTRING`/`REPLACE` expressions that are character-for-character
equivalent. They deliberately avoid string equality, because MySQL's default `utf8mb4_0900_ai_ci`
collation is case-insensitive and would otherwise accept `'usd'` and `'SHA256:…'`. No constraint was
removed, weakened, or moved to application-level validation.

**20 integration tests still fail.** Causes: MySQL reports a CHECK violation as `OperationalError` rather
than `IntegrityError`; some tests still query `pg_constraint` / `pg_attribute` or assume a `public` schema
or the `uuid`/`jsonb`/`timestamptz` physical types above. `test_the_database_matches_the_models_with_no_
pending_changes` additionally needs a dialect-aware `alembic check`. These need a decision per test —
loosening an assertion that encodes real PostgreSQL behaviour would hide a genuine gap.

**Not implemented:** real LLM provider; adjustments (no money-moving path); audit-event storage;
per-stage investigation resume; deterministic demo seed data; production authentication.

**Not verified:** the Docker Compose path; frontend↔backend end-to-end against a live MySQL server.

## 21. Security notes

**In place:** SQLAlchemy parameter binding only, no string-built SQL. Dispute descriptions are untrusted
input, capped at 10,000 characters and treated as data. The model has no capability to act — it cannot
approve, adjust, or call a tool. Payments store `method` + `last4` only; no card data. `.env` is
gitignored and `.env.example` holds placeholders only. `/health` reports the provider *mode*, never key
material.

**Not real — do not mistake this for authentication.** Identity is a pair of development headers
(`X-Actor-Id`, `X-Actor-Role`). **These are trivially spoofable.** The analyst/reviewer separation of
duties is a genuine design decision; its enforcement is a stub pending OQ-01. This is the highest-risk
assumption in the project and is flagged in the ADRs and the interview guide for that reason.

**Residual risk:** prompt injection is mitigated, not eliminated. A model can still be led to a *wrong but
well-formed, correctly-cited* conclusion. Mandatory human approval exists for that reason.

## 22. Assessment scope

Built to demonstrate judgement under a billing-correctness constraint: exact decimal arithmetic, a hard
seam between interpretation and computation, evidence that a human can check, and an AI that cannot
commit. Where a requirement was deliberately not built (adjustments, real providers), that is a decision
recorded with its reasoning rather than an omission.

27 ADRs in [`docs/ENGINEERING_DECISIONS.md`](docs/ENGINEERING_DECISIONS.md), each with the rejected
alternatives. Per-requirement status in
[`docs/REQUIREMENTS_TRACEABILITY.md`](docs/REQUIREMENTS_TRACEABILITY.md). Architecture in
[`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md), SOLID mapping in [`docs/SOLID.md`](docs/SOLID.md).

Nothing in this README is marked verified unless the command above was run and its result is recorded. No
requirement is marked "implemented and tested" on the basis of a skipped or failing test.