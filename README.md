# ResolveIQ

**AI-assisted Billing Dispute Investigation & Resolution Agent** — hiring assessment build for a fictional
SaaS company, **Northstar Cloud**.

> A customer writes: *"Our March invoice is wrong. We were charged for 40,000 API calls we never made,
> and our February payment seems to have disappeared."*

ResolveIQ investigates that complaint: it collects the invoice, contract, usage and payment records as
evidence, uses an LLM to interpret that evidence, computes every monetary amount with exact decimal
arithmetic, cites the evidence behind each finding, proposes possible causes and remedies — and then
**stops and waits for a human to approve anything**.

---

## The one idea

> **The model chooses which rule might apply. Deterministic code decides what the rule means in money.**

Two different kinds of work hide in that complaint:

| | Done by | Why not the other way round |
| --- | --- | --- |
| *Was the invoice arithmetically correct under the contract?* | Pure `Decimal` functions | An LLM doing arithmetic is non-deterministic and confidently wrong |
| *Which explanation fits this evidence?* | LLM, citing evidence keys | Deterministic code cannot read a complaint and recognise "we cancelled in January" |

The seam between them is a **typed vocabulary**: the model emits a `hypothesis_code`, a registry maps it
to a pure calculator, and the LLM response schema **has no field capable of holding a monetary amount**.
Separation is enforced by the parser, not by prompt wording.

Full reasoning: [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) §1, [`docs/ENGINEERING_DECISIONS.md`](docs/ENGINEERING_DECISIONS.md) ADR-003/004.

---

## ⚠️ Current status — read this first

Two slices are genuinely complete: the database foundation and the deterministic billing engine. The
AI interpretation workflow is now built and verified offline, but it has no HTTP API, no persistence
and no real model behind it.

| Area | Status |
| --- | --- |
| Documentation (7 documents) | Drafted |
| `Money` value object + 55 unit tests | **Implemented, verified** |
| FastAPI app factory + `/api/v1/health` | **Implemented, verified** |
| Docker Compose file | **Syntax validated; never actually started** (no Docker daemon on this machine) |
| SQLAlchemy models, Alembic migrations, seed data | **Implemented** — 62 integration tests written, **never executed against a live server** (none reachable) |
| Pricing engine (rules, traces, recalculation, reconciliation) | **Implemented, verified** — 221 tests, no database required (ADR-022) |
| Evidence model, LLM schema, citation validator | **Implemented, verified** — 83 tests |
| Impact registry (all 9 hypothesis codes) | **Implemented, verified** — 54 tests; dispatches to the engine, computes no money (ADR-023) |
| `MockLlmProvider` | **Implemented, verified** — 32 contract tests run against it |
| Investigation service (end-to-end, in memory) | **Implemented, verified** — 47 tests; no API route, no persistence, no resume |
| Real LLM provider | **Not built** — port and mock only (OQ-08, ADR-026) |
| Investigation persistence / state machine resume | Not built — findings are not stored |
| Review / adjustment services | Not built |
| `app/api/` beyond `/health` | Not built |
| Frontend (entirely) | Not built — `frontend/` is an empty directory |

Per-requirement status is tracked in
[`docs/REQUIREMENTS_TRACEABILITY.md`](docs/REQUIREMENTS_TRACEABILITY.md). No requirement is marked
"Implemented + tested" unless a command in this README was actually run and passed.

---

## Quickstart

### Prerequisites

Python 3.10+ (developed against 3.10.9; Docker image uses 3.12), and Docker Desktop for Postgres.

### With Docker (intended path)

```bash
cp .env.example .env          # never commit the copy; .env is gitignored
docker compose up db api      # frontend is not scaffolded yet, so it is left out
curl http://localhost:8000/api/v1/health
```

### Without Docker (fastest way to see the tests run)

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# .venv/bin/pip install -e ".[dev]"                    # macOS / Linux
.venv/Scripts/python.exe -m pytest
```

> `make` targets are provided for convenience, but **GNU Make is not required and was not available on
> the machine this was built on**. Every command below is the raw equivalent.

---

## Verify the setup

These are the exact commands that were run, and their actual results on 2026-10-03 (Python 3.10.9,
Windows). Re-run them to reproduce.

| # | Command (from `backend/`) | Result |
| --- | --- | --- |
| 1 | `python -m venv .venv` | created |
| 2 | `.venv/Scripts/python.exe -m pip install -e ".[dev]"` | success |
| 3 | `.venv/Scripts/python.exe -m pytest` | **59 passed** |
| 4 | `.venv/Scripts/python.exe -m ruff check app tests` | **All checks passed!** |
| 5 | `.venv/Scripts/python.exe -m ruff format --check app tests` | **17 files already formatted** |
| 6 | `.venv/Scripts/python.exe -m mypy app` | **Success: no issues found in 10 source files** (strict mode) |
| 7 | `.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8123` then `curl .../health` | **HTTP 200**, `{"status":"ok",...,"llm_provider":"mock"}` |
| 8 | `docker compose config --services` (repo root) | `db`, `api`, `web` — file is valid |
| 9 | `docker compose up -d db api` | ❌ **FAILED** — Docker daemon not running on this machine. The stack has **never** been started. |

Verification #9 is the honest gap: the container images have not been built and the Compose stack has not
been exercised. Treat `docker compose up` as untested.

---

## Repository structure

```
.
├── README.md                     ← you are here
├── AGENT_USAGE.md                ← tools, prompts, delegation, mistakes, verification
├── .env.example                  ← placeholders only; no secrets
├── .gitignore
├── docker-compose.yml            db + api + web (web not usable yet)
├── Makefile                      optional; raw commands documented above
├── docs/
│   ├── SYSTEM_DESIGN.md          architecture, entities, invariants, API, failure modes, security
│   ├── SOLID.md                  SOLID mapped to real classes + where I refused to apply it
│   ├── ENGINEERING_DECISIONS.md  17 ADRs with trade-offs and rejected alternatives
│   ├── INTERVIEW_GUIDE.md        narrative walkthrough and expected questions
│   └── REQUIREMENTS_TRACEABILITY.md
├── backend/
│   ├── pyproject.toml            deps, pytest, ruff, mypy (strict)
│   ├── Dockerfile
│   ├── app/
│   │   ├── main.py               app factory + /api/v1/health          [implemented]
│   │   ├── config.py             typed settings from env               [implemented]
│   │   ├── domain/money.py       exact-decimal value object            [implemented]
│   │   ├── api/                  HTTP layer            (Phase 1)
│   │   ├── services/             use-case orchestration (Phase 2)
│   │   ├── pricing/              deterministic engine    (Phase 2)
│   │   ├── ports/                protocols               (Phase 2)
│   │   └── adapters/             SQLAlchemy, LLM mocks   (Phase 1–2)
│   └── tests/{unit,integration,contract,fixtures}/
└── frontend/                     empty; scaffolded in Phase 4
```

**Layer rule (enforced in Phase 1 by an import check):** `domain/` and `pricing/` import nothing from
`adapters/`, `api/`, SQLAlchemy, or FastAPI. That is what lets the money engine be tested with no
database and no mocks.

---

## Implementation phases

Each phase has acceptance criteria that must be *demonstrably* met before moving on.

### Phase 0 — Foundation ✅ *(current)*
Docs, repo layout, `Money` value object, app factory, tooling.
**Done when:** `pytest`, `ruff check`, `mypy --strict` all pass; `/health` returns 200. *(Met.)*

### Phase 1 — Persistence & evidence
SQLAlchemy models (`NUMERIC` money, JSONB snapshots), Alembic migration, deterministic seed data,
`EvidenceService` (snapshot + fingerprint), `LlmProvider` port + `MockLlmProvider`.

**Acceptance criteria**
- [ ] `alembic upgrade head` then `downgrade base` both succeed on a clean Postgres
- [ ] Seed data includes one invoice with a **hand-computed** expected discrepancy
- [ ] `tests/unit/` imports nothing from `adapters/` (AST check)
- [ ] Re-adding identical evidence is a no-op; adding new evidence changes the fingerprint
- [ ] Every monetary column is `NUMERIC(19,4)`; no `FLOAT`/`REAL` anywhere

### Phase 2 — Deterministic pricing + AI interpretation
`pricing/` calculators with `calculation_trace`, LLM response schema with **no money fields**,
`EvidenceCitationValidator`, investigation stage machine.

**Acceptance criteria**
- [x] Every `hypothesis_code` calculator passes golden fixtures with hand-computed values
- [x] A test asserts `float` never appears in the `pricing/` call path
- [x] Schema rejects an unknown field; a money field cannot be expressed in the schema at all
- [x] A response citing a non-existent evidence key is **rejected entirely**
- [ ] Killing the process mid-investigation and resuming completes without duplicating rows

The pricing half of this phase is done: `PER_UNIT`, `TIERED` and `COMMITMENT` all produce exact
amounts, a single rounding at the line boundary, and an ordered `calculation_trace` naming the
contract term. Recalculating the §6.4 worked example returns 21.86 against a recorded 28.40.

The AI interpretation half is now built as a pure, offline workflow — `app/domain/evidence.py`,
`app/domain/hypotheses.py`, `app/ports/interpretation.py`, `app/ports/llm.py`,
`app/services/citations.py`, `app/services/evidence_collection.py`, `app/services/investigation.py`,
`app/pricing/impact.py` and `app/adapters/llm/mock.py`. `InvestigationService.investigate()` runs the
deterministic engine, hands the evidence bundle to an `LlmProvider`, validates the response against
the schema and then against the allowlist, computes the impact from the hypothesis code, and returns a
fingerprint and provenance.

Three things are deliberately *not* claimed. There is no real provider, so nothing here has been run
against a hosted model. There is no persistence, so an investigation cannot be resumed after a
process dies — the last criterion above is unchecked for that reason. And narrative text is not
screened for currency-shaped tokens: a model can still *say* "the amount was wrong", it simply cannot
put a figure into the structure where it would reach a calculation.

### Phase 3 — Review & adjustment (the hard requirements)
Review decisions, adjustment creation with all five duplicate-prevention layers, staleness, reopening,
audit log.

**Acceptance criteria**
- [ ] **Concurrency test:** 2 threads + barrier both POST an adjustment → exactly one `201`, one `409`
- [ ] Adjustment without an `APPROVE` review → `409 NO_APPROVED_REVIEW`
- [ ] Reusing an `Idempotency-Key` returns the original adjustment, not a second row
- [ ] Adding evidence after approval → `409 INVESTIGATION_STALE`
- [ ] Reopening a resolved dispute creates a new investigation and retains the old findings
- [ ] The application DB role cannot `UPDATE`/`DELETE` `audit_events`

### Phase 4 — Frontend
React + TS + Vite + Tailwind + shadcn/ui workbench: dispute list, evidence panel, findings, hypotheses
with calculation trace, review dialog, audit timeline.

**Acceptance criteria**
- [ ] No secret in the built bundle (verified by grepping `dist/`)
- [ ] Money is **displayed** from server values, never computed client-side
- [ ] Stale/degraded states are visibly distinct from clean results
- [ ] Frontend component tests pass

### Phase 5 — Hardening
CI, end-to-end journey test, prompt-version tooling, docs brought in line with the code.

**Acceptance criteria**
- [ ] CI runs `ruff`, `mypy --strict`, `pytest`, frontend tests on every push
- [ ] Secret scanning enabled
- [ ] `REQUIREMENTS_TRACEABILITY.md` has no unexplained `Not started` rows

---

## Security

**Real controls in place**

- No credentials in frontend code. The LLM key is backend-only; `/api/v1/health` returns the provider
  *mode* (`mock`), never key material. A test asserts a planted secret does not appear in the response.
- `.env` is gitignored; `.env.example` contains placeholders only.
- Dispute descriptions are untrusted input: capped at 10,000 characters, treated as data, and the model
  has **no capability to act** — it cannot approve, adjust, or call a tool.
- SQL injection: SQLAlchemy parameter binding only.
- PCI: payments store `method` + `last4`; no card data.

**NOT real — do not mistake this for authentication**

Identity is a pair of dev-mode headers (`X-Actor-Id`, `X-Actor-Role`) with `analyst`/`reviewer` roles.
**These are trivially spoofable.** The *separation of duties* between analyst and reviewer is a real
design decision; its *enforcement* is a stub pending an answer to OQ-01. This is the highest-risk
assumption in the project and is flagged in `docs/ENGINEERING_DECISIONS.md` and `docs/INTERVIEW_GUIDE.md`
§5 for exactly that reason.

**Residual risk:** prompt injection is mitigated, not eliminated. A model can still be led to a *wrong
but well-formed, correctly-cited* conclusion. Human approval is mandatory for that reason.

---

## Conventions

- **Money:** `Money` only. Never a bare `Decimal` for an amount, never a `float`. `float` raises
  `UnsupportedAmountTypeError` at construction — see `app/domain/money.py`.
- **Rounding:** always explicit. `pricing/rounding.py` owns which mode applies at which boundary.
- **Evidence citations:** human-readable keys (`invoice:INV-2026-03-0042#line:LI-0007`), never surrogate
  IDs.
- **API errors:** stable machine-readable `code` + human `message` + structured `details` + `request_id`.
- **Commits:** not created by the agent unless asked.

---

## Assumptions and open questions

Ten open questions are tracked in `docs/ENGINEERING_DECISIONS.md` §"Open questions". The ones that change
behaviour rather than style:

| OQ | Question | Default assumed |
| --- | --- | --- |
| OQ-01 | Where does analyst/reviewer identity come from? | Dev headers (demo-grade) |
| OQ-02 | Are adjustments pre-tax or post-tax? | Pre-tax; tax not recomputed |
| OQ-03 | `ROUND_HALF_UP` or `ROUND_HALF_EVEN`? | `HALF_UP` at invoice boundaries, `HALF_EVEN` for splits |
| OQ-04 | Multi-currency / FX in scope? | Single currency per account; cross-currency rejected |
| OQ-06 | Retention / PII policy for dispute text? | Indefinite in dev |
| OQ-09 | Volume and latency targets? | Demo scale; synchronous, no job queue |

Each is marked `DERIVED` or `ASSUMPTION` in the traceability matrix rather than presented as a requirement.
