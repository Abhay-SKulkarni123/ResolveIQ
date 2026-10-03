# System Design — ResolveIQ

**Billing Dispute Investigation & Resolution Agent (Northstar Cloud scenario)**

Status: **Foundation draft.** This document describes intended architecture. Only the pieces marked
*[implemented]* actually exist and have been run. Everything else is a design proposal awaiting review.

---

## Table of contents

1. [Problem statement and the one idea that shapes everything](#1-problem-statement-and-the-one-idea-that-shapes-everything)
2. [Architectural overview](#2-architectural-overview)
3. [Domain model](#3-domain-model)
4. [Repository structure](#4-repository-structure)
5. [Evidence and the investigation pipeline](#5-evidence-and-the-investigation-pipeline)
6. [Money: the deterministic core](#6-money-the-deterministic-core)
7. [The AI boundary](#7-the-ai-boundary)
8. [Findings, causes, resolutions](#8-findings-causes-resolutions)
9. [Review and adjustments](#9-review-and-adjustments)
10. [Failure modes](#10-failure-modes)
11. [API boundary](#11-api-boundary) — includes the [security posture](#security-posture)
12. [Frontend](#12-frontend)
13. [Invariants](#13-invariants)
14. [Data volume assumptions](#14-data-volume-assumptions)
15. [Testing strategy](#15-testing-strategy)
16. [Implementation sequence](#16-implementation-sequence)
17. [What is actually built right now](#17-what-is-actually-built-right-now)

---

## 1. Problem statement and the one idea that shapes everything

A billing analyst receives: *"Our March invoice is wrong. We were charged for 40,000 API calls we never
made, and our February payment seems to have disappeared."*

Two very different kinds of work are hidden in that sentence:

| Work | Nature | Failure mode if done by the wrong tool |
| --- | --- | --- |
| **Was the invoice arithmetically correct?** Did 40,000 calls really occur at $0.0007 each under a contract with 10,000 included calls and a 3-tier overage schedule? Did a $4,200 payment get applied? | Arithmetic over rules and records | An LLM doing arithmetic in its head is non-deterministic and confidently wrong. Not acceptable in a billing system. |
| **Which of the plausible explanations fits this evidence?** Do the usage events cluster outside business hours? Is there an unapplied payment? Is the dispute text describing something the data contradicts? | Interpretation of unstructured evidence | Deterministic code cannot read a customer's complaint and recognise "we think we cancelled in January". |

> **The organising principle of ResolveIQ:** *the model chooses which rule might apply; deterministic code
> decides what the rule means in money.*

Everything below is a consequence of that sentence. In particular:
- The LLM output schema contains **no monetary fields at all** (not "we trust ourselves not to use them").
- The LLM names a `hypothesis_code`; a registry maps that code to a pure `Decimal -> Decimal` calculator.
- Every number a human sees is accompanied by a `calculation_trace` explaining its inputs.

---

## 2. Architectural overview

A **hexagonal (ports & adapters)** backend with a **layered domain**. Chosen over a heavier framework
(Clean Architecture / DDD aggregates) because the domain is small but the *boundaries* matter more than
the entity count. See `ENGINEERING_DECISIONS.md` ADR-002.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  frontend/  React + TS + Vite + Tailwind + shadcn/ui                          │
│  Analyst workbench: dispute list, evidence panel, findings, causes,           │
│  resolution options, review dialog, audit trail.                             │
│  ⚠ Never calls an LLM provider. Never holds a credential.                     │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │ HTTPS, JSON, /api/v1
┌───────────────────────────────▼──────────────────────────────────────────────┐
│  backend/  FastAPI                                                        │
│                                                                              │
│  api/          thin HTTP adapters: routing, auth dependency, error mapping,   │
│                request/response schemas. No business logic. No SQL. No money.│
│  ──────────────────────────────────────────────────────────────────────────  │
│  services/     application services: orchestrate use cases, own transactions, │
│                enforce authorisation + state transitions.                    │
│  ──────────────────────────────────────────────────────────────────────────  │
│  domain/       entities, value objects (Money, EvidenceRef), state machines, │
│                invariants. Pure Python. No framework imports.                 │
│  pricing/      the deterministic calculation engine (pure functions).         │
│  ──────────────────────────────────────────────────────────────────────────  │
│  ports/        protocols the domain needs: LlmProvider, repositories.        │
│  adapters/     implementations: SQLAlchemy repositories, MockLlmProvider,     │
│                future real LLM adapter.                                       │
└───────────────┬──────────────────────────────┬───────────────────────────────┘
                │ SQLAlchemy                   │ LlmProvider protocol
┌───────────────▼──────────────┐  ┌────────────▼─────────────────────────────┐
│ PostgreSQL                  │  │ MockLlmProvider (deterministic, default) │
│ SQLAlchemy 2.0 + Alembic    │  │ RealLlmProvider      (env-gated, later)  │
│ NUMERIC money, JSONB snaps  │  └──────────────────────────────────────────┘
└──────────────────────────────┘
```

**The dependency rule:** arrows point inward. `domain/` imports nothing from `adapters/`, `api/`,
SQLAlchemy, or FastAPI. This is what makes the money engine and the state machines unit-testable with no
database, no network, and no mocking framework.

### Request flow for the core use case

```
POST /api/v1/disputes/{id}/investigations
  │
  ├─ InvestigationService.start_or_resume()
  │    stage COLLECT_EVIDENCE   → EvidenceService.snapshot_bundle()      [DB, deterministic]
  │    stage INTERPRET          → LlmProvider.structured_infer(...)     [LLM, validated]
  │                              └─ EvidenceCitationValidator           [pure, hard-fail]
  │    stage COMPUTE_IMPACT     → ImpactCalculator for each hypothesis  [pure Decimal]
  │    stage PROPOSE_RESOLUTIONS→ ResolutionPlanner                    [pure]
  │    stage FINALISE           → persist + audit + fingerprint
  │
  └─ 201 { investigation_id, status, degraded, findings_count, ... }
```

Each stage writes its own outcome before the next begins. That is what makes a partial failure resumable
(§10 (F10)) rather than all-or-nothing.

---

## 3. Domain model

### 3.1 Entity relationships

```
Account ─┬─< Contract ──< ContractPriceTerm
         │
         ├─< Invoice ──< InvoiceLineItem
         ├─< UsageEvent
         ├─< Payment ──< PaymentAllocation >── Invoice
         │
         └─< Dispute ─┬─< EvidenceItem          (immutable bundle)
                      ├─< Investigation ─┬─< Finding
                      │                  ├─< Hypothesis ──< ImpactBreakdown
                      │                  └─< ResolutionOption
                      ├─< ReviewDecision >── ResolutionOption
                      ├─< Adjustment >──────── ReviewDecision
                      └─< AuditEvent            (append-only)
```

### 3.2 Source-system entities (evidence inputs)

These mirror Northstar Cloud's billing systems. They are **ingested, not live-queried**, so an
investigation is reproducible against the exact records it saw (FR-002).

| Entity | Purpose | Notable columns |
| --- | --- | --- |
| `accounts` | Customer and its billing currency | `external_id`, `name`, `currency` |
| `contracts` | Effective-dated commercial agreement | `effective_from`, `effective_to`, `status` |
| `contract_price_terms` | **The pricing rules.** One row per metered metric | `metric_key`, `billing_mode`, `unit_price`, `included_units`, `tier_schedule` (JSONB), `overage_price`, `minimum_commitment` |
| `invoices` | Billing document | `period_start/end`, `currency`, `stated_total`, `computed_total`, `amount_paid`, `balance` |
| `invoice_line_items` | What the customer was actually charged | `line_type`, `metric_key`, `quantity`, `unit_price`, `amount`, `pricing_term_id` |
| `usage_events` | Metered usage records | `occurred_at`, `quantity`, `unit`, `dimensions` (JSONB), `dedupe_key` (unique) |
| `payments` | Money received | `amount`, `received_at`, `method`, `status`, `last4` (never full card data) |
| `payment_allocations` | **Which invoice a payment was applied to.** This is what "my payment disappeared" actually means | `payment_id`, `invoice_id`, `amount` |

`payment_allocations` is called out because the second half of the customer's complaint is a
*reconciliation* question, not a *pricing* question. If a payment exists but has no allocation row, the
cause is "unapplied payment", the impact is the payment amount, and the resolution is "apply payment /
reduce balance" — no LLM needed. A weak system would send that to the model anyway.

`invoices` deliberately stores **both** `stated_total` and `computed_total`. When they differ, that
difference is not an exception — it is a reconciliation finding, and it is often the answer
(PST-04/FR-005).

#### Resolved and open schema questions

`accounts`, `contracts` and `contract_price_terms` are implemented. The other five tables above are
Phase 2 work and are deliberately absent rather than stubbed.

**Resolved: `rounding_mode` is not a column.** The table above listed it on `contract_price_terms`,
which contradicted §6.2. A column implies the rounding rule is configured per contract, while §6.2
states there is exactly one rounding policy, in one module, pinned by tests. Both cannot hold. The
column was dropped rather than added and left unread: a nullable column that nothing reads is worse
than no column, because it looks like configuration. Rounding is applied by `app/pricing/rounding.py`
and nowhere else.

**Resolved: identifiers.** Each table has a surrogate `uuid` primary key generated in Python, and the
source system's own identifier lives in `external_id` with a uniqueness constraint. The surrogate keeps
our schema independent of theirs (the same reasoning as ADR-017) and means a re-ingest that finds two
rows claiming one source identifier has distinct rows to reconcile rather than silently overwriting.

**Resolved: deletes are `RESTRICT`.** Every foreign key states its delete behaviour explicitly. A
cascade would destroy the financial history a dispute is about as a side effect of tidying up a parent
row. Deleting an account that still has contracts is an error somebody must handle deliberately.

**Open: the legal values of `status` and `billing_mode`.** Neither is enumerated anywhere in the design.
`app/domain/contracts.py` defines the minimum set the system can act on — `DRAFT`, `ACTIVE`,
`SUPERSEDED`, `TERMINATED` and `PER_UNIT`, `TIERED`, `COMMITMENT` — and the database `CHECK`
constraints are generated from those enums so the two cannot drift. **These are assumptions, not
findings.** If the real billing system uses a different vocabulary, the enum and one migration are the
only things that change. They are stored as `CHECK`-constrained strings rather than native enum types
precisely so that correction is cheap.

**Open: `external_id` is unique on its own rather than per parent.** Source systems generally mint
contract identifiers globally, but no document in the repository guarantees it. If they are only unique
within an account, `contracts` needs `UNIQUE (account_id, external_id)`.

**Open: the shape of `tier_schedule`.** The database checks only that a ladder is present exactly when
`billing_mode = 'TIERED'`. The ladder's internal structure is left to the domain, because putting it in
a `CHECK` would duplicate the tier semantics in SQL and in Python and guarantee the two eventually
disagree. The trade-off is accepted: a malformed ladder is rejected by the pricing layer rather than by
the database.

#### Money and currency storage

Monetary columns are `NUMERIC(19,4)` (ADR-011) and map to `Decimal`, never to `float` and never to the
`Money` value object. `Money` belongs to the domain and is what the pricing layer computes with;
mapping it onto a column would tie the storage format to the domain type and drag domain validation into
every load path.

Currency is stored explicitly wherever money is stored: `accounts.currency` is the settlement currency
and `contract_price_terms.currency` is the currency of the amounts on that row. ResolveIQ performs no FX
conversion (OQ-04), so the two are expected to agree, but that equality is a domain rule and not a
cross-table database constraint: enforcing it in the database would require a trigger and would put
billing logic in the storage layer. Currency codes are three uppercase letters with a format check
rather than a foreign key, so an unrecognised ISO 4217 code can be stored instead of being rejected at
the boundary.

### 3.3 Investigation entities (ours)

| Entity | Purpose | Key columns |
| --- | --- | --- |
| `disputes` | The case | `status`, `severity`, `description` (untrusted), `current_investigation_id`, `version` |
| `evidence_items` | Immutable per-dispute snapshot of one source record | `evidence_type`, `natural_key`, `content_hash`, `snapshot` (JSONB), `captured_at`. Unique on `(dispute_id, natural_key, content_hash)` |
| `investigations` | One analysis run | `version`, `status`, `evidence_fingerprint`, `llm_provider`, `llm_model`, `prompt_version`, `stage_status` (JSONB), `degraded_reason`, `expires_at` |
| `findings` | An observation about the evidence | `code`, `severity`, `narrative`, `confidence` NUMERIC, `supporting_evidence` (JSONB key list), `unverified_claims` (JSONB) |
| `hypotheses` | A candidate cause | `code`, `likelihood`, `status`, `supporting_evidence`, `refuting_evidence`, `computed_impact` |
| `resolution_options` | A candidate remedy | `option_type`, `computed_amount`, `calculation_trace` (JSONB), `status` |
| `review_decisions` | Human judgement | `decision`, `reviewer_id`, `reviewer_role`, `notes`, `evidence_fingerprint_seen` |
| `adjustments` | The mock money movement | `amount`, `status`, `idempotency_key` (unique), `review_decision_id` (unique) |
| `audit_events` | Append-only history | `entity_type`, `entity_id`, `action`, `actor`, `payload`, `request_id` |

### 3.4 Dispute state machine

```
                 ┌──────────┐
                 │   OPEN   │
                 └────┬─────┘
                      │ investigate
                      ▼
              ┌───────────────┐   evidence changed / reviewer asks for more
              │ INVESTIGATING │◄──────────────────────────────┐
              └────┬──────────┘                               │
                   │ analysis complete                         │
                   ▼                                          │
           ┌───────────────┐   new evidence                  │
           │ AWAITING_REVIEW├───────────────────────────────►┘
           └───┬───────┬───┘
     approve    │       │  reject
                ▼       ▼
        ┌───────────┐  ┌──────────┐
        │ RESOLVED  │  │ REJECTED │
        └─────┬─────┘  └────┬─────┘
              │  customer disputes the outcome
              └──────► REOPENED ──► INVESTIGATING
```

Guards (enforced in the service layer, not the UI):
- `RESOLVED`/`REJECTED` → only `REOPENED` (with a mandatory reason).
- `REOPENED` → always starts a **new** `investigations` row; prior findings are retained, never mutated.
- `AWAITING_REVIEW` → cannot re-investigate while a pending `APPROVE` decision exists.

### 3.5 Investigation stage machine

```
PENDING → COLLECTING_EVIDENCE → INTERPRETING → COMPUTING_IMPACT
        → PROPOSING_RESOLUTIONS → COMPLETE
                     │
                     ├── any stage fails, retries exhausted ──► PARTIAL_FAILED ──► (resume)
                     └── evidence missing / LLM unavailable  ──► DEGRADED (still COMPLETE-with-caveat)
```

`COMPLETE`, `PARTIAL_FAILED`, and `DEGRADED` are distinct on purpose. FR-012: a degraded investigation
must never be presented as a clean one.

---

## 4. Repository structure

```
.
├── README.md
├── AGENT_USAGE.md
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Makefile                        # convenience only; GNU Make not required (see README)
├── docs/
│   ├── SYSTEM_DESIGN.md             ← you are here
│   ├── SOLID.md
│   ├── ENGINEERING_DECISIONS.md
│   ├── INTERVIEW_GUIDE.md
│   └── REQUIREMENTS_TRACEABILITY.md
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml               # deps + pytest + ruff + mypy config
│   ├── alembic.ini
│   ├── migrations/versions/         # Alembic revisions
│   ├── scripts/seed_demo_data.py    # deterministic Northstar Cloud fixture
│   ├── app/
│   │   ├── main.py                  # FastAPI factory
│   │   ├── config.py                # pydantic-settings, reads env
│   │   ├── api/                     # routers/, schemas/, deps.py, errors.py
│   │   ├── domain/                  # entities.py, money.py, state_machines.py, evidence.py
│   │   ├── pricing/                 # engine.py, rules/*.py, rounding.py
│   │   ├── services/                # dispute.py, investigation.py, review.py, adjustment.py, audit.py
│   │   ├── ports/                   # llm.py, repositories.py
│   │   └── adapters/
│   │       ├── persistence/         # SQLAlchemy models, repos, unit_of_work
│   │       └── llm/                 # mock.py, (real.py later)
│   └── tests/
│       ├── unit/                    # pure: money, pricing, state machines, validators
│       ├── integration/             # DB-backed, real Postgres via Compose
│       ├── contract/                # LlmProvider conformance, run against every provider
│       └── fixtures/                # builders for accounts, invoices, disputes
└── frontend/
    ├── package.json, vite.config.ts, tsconfig.json, tailwind.config.ts
    └── src/
        ├── app/                     # router, layout, providers
        ├── features/disputes/
        ├── features/investigation/
        ├── features/review/
        ├── components/ui/           # shadcn/ui
        └── lib/                     # api client, types, money display helpers
```

### Layer rules

| Layer | May import | Must not import |
| --- | --- | --- |
| `domain/` | stdlib, `decimal`, `pydantic` (for value validation only) | SQLAlchemy, FastAPI, `adapters/`, `services/` |
| `pricing/` | `domain/` | anything else |
| `services/` | `domain/`, `pricing/`, `ports/` | `api/`, `adapters/` |
| `api/` | `services/`, `domain/` | `pricing/` internals, raw SQL |
| `adapters/` | `domain/`, `ports/`, SQLAlchemy, HTTP clients | `services/` |

Enforced by an import-linter style check added in Phase 1 (a plain pytest test that walks the AST is
enough — no extra dependency).

---

## 5. Evidence and the investigation pipeline

### 5.1 What counts as evidence

`evidence_items` is the **only** vocabulary a finding may cite. Evidence types:

| Type | `natural_key` example |
| --- | --- |
| `INVOICE` | `invoice:INV-2026-03-0042` |
| `INVOICE_LINE` | `invoice:INV-2026-03-0042#line:LI-0007` |
| `USAGE_SUMMARY` | `usage:api_calls:2026-03-01..2026-03-31` |
| `USAGE_EVENT` | `usage_event:UE-993214` |
| `PAYMENT` | `payment:PAY-778812` |
| `PAYMENT_ALLOCATION` | `allocation:PAY-778812→INV-2026-03-0042` |
| `CONTRACT_TERM` | `contract:CTR-5512#term:api_calls` |
| `DISPUTE_TEXT` | `dispute_text:DSC-000123` |

`natural_key` is a stable, human-readable identifier. Findings cite these strings — never row IDs — so a
finding stays meaningful in a screenshot or a ticket.

### 5.2 The evidence bundle is immutable

At `COLLECTING_EVIDENCE` each source record is copied into `evidence_items.snapshot` (JSONB) and hashed
(`content_hash` = SHA-256 of a canonical JSON serialisation). Consequences:

- Re-running the investigation later uses the **same** snapshots unless new evidence is explicitly added.
- Every investigation stores an `evidence_fingerprint` = SHA-256 over the sorted `(natural_key,
  content_hash)` pairs. This single value drives staleness detection (§9.2 and §10 (F5/F6)) and approval validity (§10.2).
- Adding the same evidence twice is a no-op (unique constraint), so the "add evidence" flow is idempotent.

### 5.3 Citations are validated, structurally

`EvidenceCitationValidator` is a pure function over `(model_response, allowed_keys) -> ValidatedResponse`:

1. Every key in `supporting_evidence`, `refuting_evidence`, and `hypothesis.supporting_evidence` must be
   in the case bundle's allowed key set.
2. Every `finding` must cite **at least one** key. A finding with no citation is rejected — that is the
   "never treat unsupported claims as verified" rule expressed as a schema constraint.
3. Any statement the model marks as inference rather than observation must go in `unverified_claims`,
   which the API returns in a **separate field** with a distinct type. The UI cannot render it as fact
   because it is not shaped like a fact.
4. One invalid key ⇒ **the entire response is rejected**, not silently filtered. Silently dropping
   citations would let a hallucination hide inside an otherwise-valid answer.

Trade-off: strict rejection means one bad citation loses the whole run. Chosen deliberately — a partial
finding set that has been quietly laundered is harder to review than an obvious failure. Mitigated by the
repair-retry in §10 (F10).

---

## 6. Money: the deterministic core

### 6.1 `Money` value object *[implemented]*

`backend/app/domain/money.py`. The only type allowed to hold an amount.

```python
@dataclass(frozen=True, order=True)
class Money:
    amount: Decimal
    currency: str          # ISO 4217, uppercase, validated

    def __post_init__(self):
        # rejects float  -> raises TypeError
        # rejects NaN / Infinity
        # rejects currency that is not 3 uppercase letters
```

Rules encoded here:
- **`float` is rejected outright**, not coerced. `Decimal(0.1)` is exactly `0.1000000000000000055511151231…`;
  `Decimal(str(0.1))` papers over a problem that should be impossible. Rejection makes the bug loud.
  At the HTTP boundary, JSON numbers arrive as strings or ints in our schema (Pydantic parses to
  `Decimal`), and any residual float is converted with `Decimal(repr(x))` **and flagged**.
- All arithmetic goes through `+ - *` returning `Money`, and helpers that require an explicit
  `rounding=RoundingPolicy` argument. There is no implicit rounding.
- Comparison across currencies raises. Adding different currencies raises. No implicit FX (OQ-04).
- `Decimal` context: the global context is left alone; every operation that needs a precision explicitly
  uses `localcontext()`. Global mutation of the decimal context is a classic source of
  order-dependent bugs that only appear under concurrency.

Storage: `NUMERIC(19,4)`. Four decimal places retained (unit prices are sub-cent, e.g. $0.0007 per API
call), quantisation to 2 dp happens only at presentation and settlement boundaries.

### 6.2 Rounding policy

One constant, one module (`pricing/rounding.py`), pinned by tests:

| Boundary | Mode | Rationale |
| --- | --- | --- |
| Extended/unit-price amounts | exact, no rounding | Preserve precision |
| Line item amount | `ROUND_HALF_UP` | Commercial billing convention |
| Invoice total | `ROUND_HALF_UP` | Matches the invoicing system being reconciled |
| Allocation / proration residual | `ROUND_HALF_EVEN` | Minimises systematic bias when splitting across many rows |

`ROUND_HALF_EVEN` for residual distribution is the one non-obvious choice; it is intentional and tested.
Open question OQ-03 asks the client to confirm the commercial convention.

### 6.3 Impact calculation: the LLM picks the code, the engine does the math

```
LLM output                        Deterministic engine
─────────────                    ────────────────────
Hypothesis {                      hypothesis_code ──► registry lookup
  code: "OVERAGE_TIER_MISMATCH"                     │
  title: "..."                                      ▼
  narrative: "..."                        TieredOverageCalculator
  supporting_evidence: [...]                 .evaluate(usage_summary, price_term, invoice)
  confidence: 0.82                                    │
}                                                     ▼
                                          Money + calculation_trace
```

Registry of deterministic calculators (each a pure function, each independently unit-tested):

| `hypothesis_code` | Calculator | Reads |
| --- | --- | --- |
| `OVERAGE_TIER_MISMATCH` | `tiered.py` | usage summary, `contract_price_terms.tier_schedule` |
| `UNIT_PRICE_MISMATCH` | `unit_price.py` | usage summary, `unit_price` |
| `DUPLICATED_USAGE` | `dedupe.py` | `usage_events.dedupe_key` collisions |
| `OUT_OF_PERIOD_USAGE` | `period.py` | `occurred_at` vs `invoice.period_*` |
| `UNAPPLIED_PAYMENT` | `reconcile.py` | `payments` − `payment_allocations` |
| `INCORRECT_ALLOCATION` | `reconcile.py` | allocation vs invoice balance |
| `STATEMENT_TOTAL_MISMATCH` | `reconcile.py` | `stated_total` vs `computed_total` |
| `COMMITMENT_SHORTFALL` | `commitment.py` | `minimum_commitment` vs billed |
| `UNEXPLAINED` | none | impact `None` — "needs human judgement" |

`UNEXPLAINED` matters. A good system is willing to say "I cannot determine the cause from this evidence",
and must not manufacture an impact to look complete.

### 6.4 Calculation trace

Every computed amount carries an ordered, serialisable trace:

```json
{
  "steps": [
    {"n": 1, "label": "Total metered quantity in period",
     "expression": "sum(usage_events.quantity WHERE metric_key='api_calls' AND occurred_at BETWEEN ...)",
     "inputs": {"quantity": "41234", "event_count": 41234}, "result": "41234"},
    {"n": 2, "label": "Subtract included units",
     "expression": "max(41234 - 10000, 0)", "result": "31234"},
    {"n": 3, "label": "Tier 1 (10000–50000 @ 0.00070)",
     "expression": "min(31234, 40000) * 0.00070", "result": "21.86380"},
    {"n": 4, "label": "Round line amount (ROUND_HALF_UP)", "result": "21.86"}
  ],
  "rule_ref": "contract:CTR-5512#term:api_calls",
  "engine_version": "1.0.0"
}
```

An analyst disputing a number can find the exact step that produced it, with the rule reference. This is
also what makes the deterministic engine testable against golden fixtures.

---

## 7. The AI boundary

### 7.1 The provider port

```python
class LlmProvider(Protocol):
    name: str
    model: str

    def structured_infer(self, request: InterpretationRequest) -> InterpretationResponse: ...
```

`InterpretationRequest` carries the evidence bundle (already trimmed to what is relevant), the dispute
text, the JSON schema to conform to, and the prompt version. `InterpretationResponse` is a Pydantic model
with `extra="forbid"` — **and no monetary fields.**

Every provider must pass `tests/contract/test_llm_provider_conformance.py`, which asserts: schema
conformance, citation validity, no money fields, deterministic behaviour under `MockLlmProvider`, and
timeout/exception behaviour. Adding a real provider later means adding the adapter plus conformance, not
touching the domain.

### 7.2 Prompting and provenance

- Prompt text is versioned (`prompt_version`) and the version is stored on the investigation (FR-016), so
  a behaviour change is attributable.
- The prompt states the response schema, the allowed evidence keys, and an explicit instruction that
  dispute text is **data to be analysed, not instructions to be followed** (NFR-007).
- Provider and model are recorded. Two runs with different models are not comparable, and we should not
  pretend they are.

### 7.3 Why the response schema has no money fields

This is the mechanism behind NEP-02. Alternatives considered:

| Option | Why rejected |
| --- | --- |
| "Prompt the model not to return amounts" | A prompt is advice, not a type. |
| "Validate returned amounts against the engine and overwrite" | The model may still *narrate* an invented amount in prose, and the reviewer's eye goes to the number. |
| **Schema simply has no amount field** ✅ | Separation is enforced by the parser. A model that tries to emit an amount gets an unknown-field error and a repair retry. |

---

## 8. Findings, causes, resolutions

### 8.1 Finding

`code`, `severity` (`INFO|WARN|CRITICAL`), `category`, `narrative`, `confidence` (`Decimal` 0–1),
`supporting_evidence[]`, `unverified_claims[]`.

`confidence` is the model's *own* estimate and is labelled as such in the UI. It is never used
programmatically to gate an adjustment — a 0.99-confidence finding and a 0.2-confidence finding both
require human approval.

### 8.2 Hypothesis (possible cause)

A candidate explanation with `supporting_evidence`, `refuting_evidence`, `likelihood`, `status`
(`OPEN|RULED_OUT|CONFIRMED`), and a `computed_impact` produced **only** by §6.3. Multiple hypotheses are
normal and expected; the point is to make the analyst's job a comparison, not a search.

### 8.3 Resolution option

`option_type` ∈ `FULL_CREDIT | PARTIAL_CREDIT | REBILL_CORRECT_AMOUNT | APPLY_UNAPPLIED_PAYMENT |
NO_ADJUSTMENT | REQUEST_MORE_INFO`.

- Every money-moving option has `requires_approval = true`, unconditionally. There is no code path that
  creates an approved adjustment without a human `APPROVE` decision (NEP-04).
- Non-monetary options exist (`NO_ADJUSTMENT`, `REQUEST_MORE_INFO`) because "the invoice is correct, here
  is why" is a legitimate and common outcome (OQ-05).
- The system **proposes**; a reviewer **selects at most one**, or none. The LLM never selects.

---

## 9. Review and adjustments

### 9.1 Flow

```
Investigation COMPLETE
  → reviewer opens dispute, sees evidence + findings + causes + options
  → reviewer submits decision: APPROVE(option) | REJECT | REQUEST_MORE_INFO(notes)
  → decision persisted with the evidence_fingerprint the reviewer saw
  → POST /api/v1/adjustments  { Idempotency-Key: … }
      guard: approved decision exists?           else 409 NO_APPROVED_REVIEW
      guard: decision.evidence_fingerprint_seen == investigation.evidence_fingerprint?
                                                   else 409 INVESTIGATION_STALE
      guard: investigation not expired?           else 409 INVESTIGATION_STALE
      guard: no active adjustment for dispute?    else 409 DUPLICATE_ADJUSTMENT
      guard: amount <= invoice balance?           else 422 AMOUNT_EXCEEDS_BALANCE
      → INSERT adjustment (unique constraints are the real enforcement)
      → append audit event
      → 201 Created
```

### 9.2 Approval validity

The reviewer approves *a view of the evidence*. If evidence is added afterwards, that approval is void.
This is why `review_decisions.evidence_fingerprint_seen` exists (FR-008): it makes "reopen because the
facts changed" a mechanical check rather than a policy reminder.

### 9.3 Preventing duplicate adjustments — five independent layers

NEP-05 explicitly forbids solving this with a disabled button. Layers, outermost first:

1. **Client idempotency key** — `Idempotency-Key` header, `UNIQUE` index on `adjustments.idempotency_key`.
   A retried request returns the original adjustment with `Idempotency-Replayed: true`, not a 500 and not
   a second row. Protects against network retries and double-clicks.
2. **Partial unique index — at most one *active* adjustment per dispute:**
   ```sql
   CREATE UNIQUE INDEX uq_adjustments_one_active_per_dispute
       ON adjustments (dispute_id) WHERE status IN ('PENDING','APPLIED');
   ```
   This is the layer that actually holds under concurrency, because the database is the last arbiter.
3. **Row lock** — `SELECT … FROM disputes WHERE id = :id FOR UPDATE` inside the transaction serialises
   two concurrent approvals for the same dispute. The second one then sees layer 2 and gets `409`.
4. **Optimistic concurrency** — `disputes.version` incremented on every mutation; a stale write is
   rejected with `409 VERSION_CONFLICT` instead of silently overwriting a colleague's review.
5. **State-machine guard** — `PENDING_REVIEW → RESOLVED` only via an `APPROVE` decision; a second
   `APPROVE` on an already-resolved dispute is a no-op conflict, never a second write.

The frontend disabling the button is **layer 0**: pure UX, trivially bypassed, never load-bearing. It is
kept because good UX matters, and it is documented as non-authoritative.

Why five? They fail in different ways. The idempotency key handles retries; the partial unique index
handles true concurrency; the row lock gives a clean error instead of a constraint violation; the version
column protects *other* fields from lost updates; the state guard protects the transition itself. A system
relying on any single one has a known, findable failure.

---

## 10. Failure modes

| # | Failure | Detection | Response | Residual risk |
| --- | --- | --- | --- | --- |
| F1 | LLM returns invalid JSON / wrong shape | Pydantic validation | Repair prompt (1 retry), then `DEGRADED` with reason | Reduced coverage; explicitly surfaced |
| F2 | LLM cites a non-existent evidence key | Citation validator | Reject response, repair retry, then `DEGRADED` | Never silently accepted |
| F3 | LLM provider unavailable / timeout | Adapter timeout | Backoff retries → `PARTIAL_FAILED`, resumable | Investigation incomplete; UI blocks approval |
| F4 | Evidence incomplete (e.g. usage ingest lag) | Bundle completeness check | `DEGRADED`, findings scoped to available evidence, `degraded_reason` set | Analyst must judge sufficiency |
| F5 | Evidence changes after analysis | Fingerprint mismatch | Mark `STALE`; approval blocked with `409` | Forces re-analysis — intended |
| F6 | Investigation older than TTL | `expires_at` | Same as F5 | Slightly annoying, deliberately conservative |
| F7 | Concurrent approval | Partial unique index + `FOR UPDATE` | `409 DUPLICATE_ADJUSTMENT`, existing adjustment returned | None for double-spend |
| F8 | Client retry after timeout | Idempotency key | Original result replayed | None |
| F9 | Stale write from another tab | `version` column | `409 VERSION_CONFLICT` | User must refresh |
| F10 | Crash mid-investigation | Per-stage `stage_status` | Resume from first non-completed stage | Wasted LLM calls possible; idempotent writes make it safe |
| F11 | Currency mismatch (adjustment vs invoice) | `Money` comparison guard | 422 | Explicit failure, no silent FX |
| F12 | Amount exceeds balance | Service guard | 422 `AMOUNT_EXCEEDS_BALANCE` | Policy question open (OQ-02) |
| F13 | Dispute text contains prompt injection | Schema + key allowlist + system-prompt framing | Output still schema- and citation-validated | Cannot be fully eliminated; bounded by validation |
| F14 | Prompt injection via usage `dimensions` metadata | Same | Treated as untrusted strings; never interpreted | Bounded |
| F15 | DB unavailable | Connection error | 503 + retry hint; no partial writes (transactional stages) | Investigation blocked, resumable |
| F16 | Seed/ingest data inconsistent with contract | Reconciliation at ingest | Load succeeds, reconciliation finding raised | Deliberate: inconsistency is the signal |

---

## 11. API boundary

Thin controllers: parse → authorise → delegate → serialise. No business logic, no SQL, no arithmetic.

```
POST   /api/v1/disputes                          Create dispute (ingest description + link invoice)
GET    /api/v1/disputes                          List, filter by status
GET    /api/v1/disputes/{id}                     Detail: status, staleness, current investigation
POST   /api/v1/disputes/{id}/evidence            Attach additional evidence (idempotent)
POST   /api/v1/disputes/{id}/investigations      Start or resume investigation
GET    /api/v1/investigations/{id}               Findings, hypotheses, options, evidence, trace
POST   /api/v1/investigations/{id}/review        Submit reviewer decision
POST   /api/v1/adjustments                       Create adjustment  ⚠ Idempotency-Key required
GET    /api/v1/adjustments/{id}
GET    /api/v1/disputes/{id}/audit               Audit history
GET    /api/v1/capabilities                      Provider mode + feature flags (never secrets)
GET    /api/v1/health                            Liveness/readiness
```

### Error envelope

```json
{
  "error": {
    "code": "INVESTIGATION_STALE",
    "message": "Evidence changed since this investigation was produced.",
    "details": {"expected_fingerprint": "…", "actual_fingerprint": "…"},
    "request_id": "01J…"
  }
}
```

Stable machine-readable `code` (frontend branches on it), human `message`, structured `details`,
`request_id` correlating to logs and the audit trail. Consistent errors are what let the UI show
"evidence changed, re-analyse" instead of "Something went wrong".

### Security posture

- **All credentials backend-only.** The LLM provider key lives in the API container's environment. The
  frontend never holds a provider key and never calls a provider directly. `/api/v1/capabilities` reports
  `{"llm_provider": "mock"|"configured"}` and nothing else. There is no endpoint that echoes config.
- **Identity/roles (assumption, OQ-01):** dev-mode header identity (`X-Actor-Id`, `X-Actor-Role`) with
  `analyst` and `reviewer` roles, enforced by a FastAPI dependency. This is **demo-grade and clearly
  labelled as such** — a header is trivially spoofable. Production requires real token verification. I am
  documenting this as a known gap rather than presenting it as security.
- **SQL injection:** SQLAlchemy parameter binding only; no string-built SQL anywhere.
- **PII/PCI:** no card data stored. Payments keep `method` and `last4` only. Dispute descriptions may
  contain PII — retention policy is open (OQ-06).
- **Secrets hygiene:** `.env` gitignored, `.env.example` contains placeholders only, no secrets in
  frontend `VITE_*` vars (Vite inlines those into the bundle — this is why the key must not be one).
- **Untrusted input:** dispute description capped at 10,000 chars; treated as data (NFR-007).
- **Least privilege:** the application DB role gets no `UPDATE`/`DELETE` on `audit_events` (FR-014).

---

## 12. Frontend

React + TypeScript + Vite + Tailwind + shadcn/ui. Structure mirrors the backend vocabulary.

| Screen | Purpose |
| --- | --- |
| Dispute list | Queue with status, severity, age, staleness badge |
| Dispute detail | Evidence panel · Findings · Hypotheses (with impact + trace) · Options |
| Review dialog | Select one option, approve/reject/request-info, mandatory notes on reject |
| Adjustment view | Adjustment status, amount, idempotency/replay indicator |
| Audit timeline | Append-only event stream for the dispute |

Rules:
- The frontend is a **renderer of server truth**. It never computes an amount, never derives staleness,
  and never decides whether approval is allowed — it renders what the API returns. A client-side
  recomputation would be a second, divergent implementation of the money logic.
- Money is displayed from server strings; the frontend must not do arithmetic on them.
- Optimistic UI is limited to non-authoritative affordances (disabled buttons). Every state change is
  confirmed by refetch.

---

## 13. Invariants

Machine-checkable where possible. Each maps to a test in Phase 1+.

| ID | Invariant | Enforcement |
| --- | --- | --- |
| INV-01 | No monetary value is ever a `float`. | `Money` rejects `float`; DB `NUMERIC`; AST check for float literals in `pricing/` |
| INV-02 | No LLM output field carries a monetary amount. | Schema has no such field; contract test asserts |
| INV-03 | Every finding cites ≥1 evidence key, all of which exist in the bundle. | `EvidenceCitationValidator` + DB check |
| INV-04 | Sum of `invoice_line_items.amount` is recorded and compared to `invoice.stated_total`; mismatch is a finding, not an exception. | Reconciliation service |
| INV-05 | `sum(payment_allocations.amount) <= payments.amount` per payment. | Ingest check + test |
| INV-06 | At most one adjustment in `PENDING`/`APPLIED` per dispute. | Partial unique index |
| INV-07 | An adjustment requires an `APPROVE` review decision referencing it. | FK (NOT NULL) + service guard |
| INV-08 | An adjustment's approval must have been made against the current `evidence_fingerprint`. | Service guard → `409` |
| INV-09 | Adjustment currency == invoice currency. | `Money` cross-currency guard |
| INV-10 | `audit_events` is append-only. | No UPDATE/DELETE grant for app role |
| INV-11 | Investigations are immutable once `COMPLETE`; re-analysis creates a new version. | Service guard |
| INV-12 | `computed_impact` is only ever written by `ImpactCalculator`. | Private constructor / module boundary + review |
| INV-13 | Money is quantised to currency minor units only at presentation/settlement. | `Money.quantise()` call-site review + tests |

---

## 14. Data volume assumptions

**Assumed, not measured (OQ-09).** Demo-scale: 1 account, ~3 contracts, ~5 invoices, ~10⁵ usage events,
~10 payments, a handful of disputes. Design consequences: synchronous investigation is acceptable
(no job queue); `usage_events` needs indexes on `(account_id, metric_key, occurred_at)` but no
partitioning; JSONB snapshots are small. **If real volume arrives**, the first changes would be a
background worker for investigations, aggregated usage rollups instead of raw event scans, and
partitioned `usage_events`. Flagging this rather than pre-building it.

---

## 15. Testing strategy

| Layer | Scope | Needs DB? | Needs network? |
| --- | --- | --- | --- |
| `unit/` | `Money`, `pricing/*`, state machines, citation validator, fingerprinting | No | No |
| `contract/` | Every `LlmProvider` satisfies the same conformance suite | No | No (mock) |
| `integration/` | Repos, migrations, idempotency under real concurrency, state transitions | Yes (Compose) | No |
| frontend `unit` | Components, money *formatting*, staleness badges | No | No |
| frontend e2e | Dispute → investigate → review → adjust | Yes | No |

The concurrency test in `integration/` is the important one: two threads, one barrier, both POST an
adjustment, assert exactly one `201` and one `409`. That test is the real proof of NEP-05.

---

## 16. Implementation sequence

See `REQUIREMENTS_TRACEABILITY.md` for status and `ENGINEERING_DECISIONS.md` for rationale.
Acceptance criteria per phase are in `README.md` §"Phases".

---

## 17. What is actually built right now

Being explicit so this document is not over-read:

**Phase 0 — foundation**

- ✅ `docs/*` — all seven documents drafted.
- ✅ `.env.example`, `.gitignore`, `docker-compose.yml`, `Makefile`.
- ✅ `backend/app/config.py` — typed settings from env.
- ✅ `backend/app/domain/money.py` — the `Money` value object, with `tests/unit/test_money.py`.
- ✅ `backend/app/main.py` — app factory with `/api/v1/health`.

**Phase 1, Slice 1 — persistence foundation**

- ✅ `backend/app/domain/contracts.py` — `ContractStatus` and `BillingMode`, shared by the ORM constraints
  and by whatever reads them later.
- ✅ `backend/app/adapters/persistence/` — `Base` with deterministic constraint naming, the three models,
  and the engine/session lifecycle with credential-safe failure reporting.
- ✅ `backend/alembic.ini`, `backend/migrations/` — `env.py` that reads the URL from the environment and
  never from the app, and the initial migration for the three tables.
- ✅ `backend/scripts/create_dev_database.sql` — idempotent, least-privilege role and databases.
- ✅ Tests: 145 unit (schema declarations, credential redaction, layer boundaries, settings) and 62
  integration (round trip, migration reversibility, every constraint).

**Not built:** the repository layer and session-per-request wiring, the pricing engine, the evidence
pipeline, LLM adapters, the investigation service, review/adjustment services, and the entire frontend.

**Not yet verified:** the 62 integration tests have never been executed, because the database role did not
exist when they were written. The schema is *declared and unit-tested*, not *accepted by PostgreSQL*. See
`AGENT_USAGE.md` §9.6.

The `Money` value object was built first on purpose: NEP-01 is the principle everything else leans on,
and it is the one piece where being wrong is silently expensive.
