# Requirements Traceability Matrix — ResolveIQ

**Status:** Foundation draft (Phase 0). Most implementation columns are intentionally `Not started`.
**Last updated:** Phase 0 scaffold.

## How to read this document

Every requirement has an ID and a **provenance** tag. This matters for an assessment: a reviewer must be
able to tell the difference between *what was asked for* and *what I inferred*.

| Provenance tag | Meaning |
| --- | --- |
| `BRIEF` | Stated explicitly in the assessment brief. Treat as authoritative. |
| `DERIVED` | I inferred this from the brief to make it implementable. **Needs review.** May be wrong or out of scope. |
| `ASSUMPTION` | A placeholder pending an answer to an open question. See [Open questions](#7-open-questions-blocking-these-requirements). |

Status values: `Not started` · `In progress` · `Implemented` · `Implemented + tested` · `Blocked` · `Dropped (proposed)`

Nothing in this repository is marked `Implemented + tested` unless a command in the README was actually run
and passed. See `AGENT_USAGE.md` for the verification log.

---

## 1. Product story behaviours

| ID | Requirement | Provenance | Status | Design reference | Test reference | Code |
| --- | --- | --- | --- | --- | --- | --- |
| PST-01 | Accept a customer complaint about an invoice containing unexpected usage charges | BRIEF | Not started | `SYSTEM_DESIGN.md` §3 | — | — |
| PST-02 | Investigate using invoice line items, contract/pricing rules, usage events, payment history | BRIEF | Not started | §3, §5 | — | — |
| PST-03 | Take the customer's dispute description into account as evidence | BRIEF | Not started | §5.1 | — | — |
| PST-04 | Calculate monetary amounts deterministically | BRIEF | **Implemented + tested** | §6, §6.2, §6.4 | `tests/unit/test_pricing_{rounding,rules,usage,engine,reconciliation}.py` + `test_domain_billing.py` + `test_no_float_in_calculations.py` (221 tests) | `app/pricing/`, `app/domain/billing.py` |
| PST-05 | Use an LLM to interpret supplied evidence | BRIEF | Not started | §7 | — | — |
| PST-06 | Cite evidence for every finding | BRIEF | Not started | §5.3, §8.1 | — | — |
| PST-07 | Present possible causes and resolution options | BRIEF | Not started | §8 | — | — |
| PST-08 | Require human review before a mock adjustment is approved | BRIEF | Not started | §8.3, §9 | — | — |

## 2. Non-negotiable engineering principles

These are quoted from the brief and are treated as acceptance criteria for the whole project.

| ID | Requirement (brief wording) | Provenance | Status | Design reference | Enforcement mechanism | Test reference | Code |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NEP-01 | Monetary calculations must use Python `Decimal` or equivalent exact decimal arithmetic, never binary floating-point | BRIEF | **Implemented + tested** (value object + full calculation path; API boundary still to build) | §6.1, §6.2 | `domain/money.py` and `domain/billing.py` reject `float`; DB `NUMERIC`; AST check rejects float literals, `float()`, `round()` and int/int division across `domain/` and `pricing/` | `tests/unit/test_money.py`, `test_domain_billing.py`, `test_no_float_in_calculations.py` | `app/domain/money.py`, `app/domain/billing.py`, `app/pricing/*` |
| NEP-02 | Keep financial calculations separate from AI-generated interpretations | BRIEF | In progress (separation enforced; no AI code exists yet, so nothing yet depends on it) | §6, §7 | `app/pricing/` imports only `app/domain/`; the whole calculation path is pure and takes no LLM input. The stronger half of the claim — LLM response schema has **no money fields** — cannot be shown until that schema exists | `tests/unit/test_layer_boundaries.py`, `test_no_float_in_calculations.py` | `app/pricing/*` |
| NEP-03 | AI findings must reference supplied evidence; never fabricate evidence identifiers or treat unsupported claims as verified | BRIEF | Not started | §5.3, §7.1 | `EvidenceCitationValidator` allowlists keys against the case bundle; rejects the whole response on violation; unsupported claims land in a separate `unverified_claims` field | — | — |
| NEP-04 | Human approval is required for mock adjustments | BRIEF | Not started | §8.3, §9.1 | Adjustment creation requires an `APPROVE` review decision; enforced in service + DB FK | — | — |
| NEP-05 | Prevent duplicate adjustments through transactional and database safeguards, not merely a frontend button disable | BRIEF | Not started | §9.3 | 5 independent layers: idempotency key + unique index, partial unique index on active adjustment per dispute, `SELECT … FOR UPDATE`, optimistic `version` column, state-machine guard | — | — |
| NEP-06 | Persist disputes, findings, evidence references, reviewer decisions, adjustments, and audit history | BRIEF | Not started | §3 | SQLAlchemy models + Alembic migrations; `audit_events` append-only | — | — |
| NEP-07 | Support additional evidence, case reopening, stale investigation detection, and recoverable partial failures | BRIEF | Not started | §5.2, §9.2, §10 | `evidence_items` append-only bundle; `POST /reopen`; `evidence_fingerprint` + `expires_at`; per-stage status with `resume_from` | — | — |
| NEP-08 | Validate inputs and structured LLM outputs | BRIEF | Not started | §5.3, §7.3 | Pydantic v2 strict models at every boundary; `extra="forbid"`; structured output / JSON-schema-constrained decoding; retry-then-degrade | — | — |
| NEP-09 | Never commit credentials or expose secrets in frontend code | BRIEF | In progress (`.env.example` only, no real secrets) | §11.2 | Backend-only env; `.gitignore`; frontend receives `/api/v1/capabilities` which never contains key material; CI secret scan | — | `.env.example`, `.gitignore` |
| NEP-10 | Keep the architecture simple; introduce abstractions only when they improve testability, reliability, or maintainability | BRIEF | Not started | `SOLID.md` | Every interface in `app/ports/` must name the concrete problem it solves; documented in `SOLID.md` §"Abstraction ledger" | — | — |

## 3. Preferred stack

| ID | Requirement | Provenance | Status | Notes / deviations |
| --- | --- | --- | --- | --- |
| STK-01 | Frontend: React, TypeScript, Vite, Tailwind CSS, shadcn/ui | BRIEF | Not started | — |
| STK-02 | Backend: Python, FastAPI, Pydantic | BRIEF | In progress | Pydantic v2 |
| STK-03 | Persistence: PostgreSQL, SQLAlchemy, Alembic | BRIEF | Not started | SQLAlchemy 2.0 typed `Mapped[]` style |
| STK-04 | Tests: pytest and appropriate frontend tests | BRIEF | In progress | `pytest` configured; frontend runner (Vitest) not yet chosen — see OQ-07 |
| STK-05 | Local development: Docker Compose | BRIEF | In progress | `docker-compose.yml` defines `db` + `api` + `web` |
| STK-06 | AI: injectable LLM provider interface with a deterministic mock for tests | BRIEF | Not started | `app/ports/llm.py` protocol; `MockLlmProvider` is deterministic (seeded, fixture-driven) |

## 4. Required project documentation

| ID | Requirement | Provenance | Status | File |
| --- | --- | --- | --- | --- |
| DOC-01 | System design document | BRIEF | Drafted | `docs/SYSTEM_DESIGN.md` |
| DOC-02 | SOLID document | BRIEF | Drafted | `docs/SOLID.md` |
| DOC-03 | Engineering decisions record | BRIEF | Drafted | `docs/ENGINEERING_DECISIONS.md` |
| DOC-04 | Interview guide | BRIEF | Drafted | `docs/INTERVIEW_GUIDE.md` |
| DOC-05 | Requirements traceability matrix | BRIEF | Drafted | this file |
| DOC-06 | README | BRIEF | Drafted | `README.md` |
| DOC-07 | `AGENT_USAGE.md` documenting tools, prompts, delegated work, mistakes, verification | BRIEF | Drafted | `AGENT_USAGE.md` |
| DOC-08 | `.env.example` | BRIEF | Drafted | `.env.example` |

---

## 5. Derived functional requirements (`DERIVED` — please review)

These were **not** in the brief. They exist because the brief's principles are not implementable without
them. Each is a proposal.

| ID | Requirement | Rationale | Status | Design reference |
| --- | --- | --- | --- | --- |
| FR-001 | An investigation is a versioned, re-runnable analysis over an immutable evidence bundle | Findings must be attributable to the exact evidence reviewed | Not started | §3.5, §5.2 |
| FR-002 | The evidence bundle stores an immutable snapshot (`JSONB`) plus a content hash of each item | Re-running against live source data would make findings irreproducible | Not started | §5.2 |
| FR-003 | Every finding stores `supporting_evidence` keys, and unverified statements are stored separately in `unverified_claims` | Makes "supported vs asserted" a data-model distinction, not a prose convention | Not started | §5.3 |
| FR-004 | Monetary impact is computed from a `hypothesis_code` selected by the LLM, via a deterministic registry of calculators | Enforces NEP-02 structurally: the model chooses *which* rule applies, never *how much* | Not started | §6.3 |
| FR-005 | Every computed amount carries a `calculation_trace` (ordered, human-readable steps with inputs) | An analyst must be able to audit why a number is what it is | **Implemented + tested** — trace on every resolved line, `rule_ref` naming the contract term; §6.4 worked example reproduced in a test | §6.4 |
| FR-006 | Resolution options are proposed by the system; a reviewer selects exactly one, or none | Prevents the LLM from auto-selecting a remedy | Not started | §8 |
| FR-007 | `POST /api/v1/adjustments` requires an `Idempotency-Key` header | Client-initiated retries must not create a second adjustment | Not started | §9.3 |
| FR-008 | Reviewer decisions record the `evidence_fingerprint` they were looking at | If evidence changes after approval, the approval is void | Not started | §9.2 |
| FR-009 | Approving an adjustment whose investigation is stale returns `409 INVESTIGATION_STALE` | Directly serves NEP-07 | Not started | §9.1, §10 (F5/F6) |
| FR-010 | An adjustment may not exceed the invoice balance outstanding at approval time | Prevents over-crediting; needs policy confirmation | Not started | §9.1, §10 (F12) |
| FR-011 | Investigation stages are individually recorded so a failed run can resume from the first failed stage | Serves "recoverable partial failures" in NEP-07 | Not started | §3.5, §10 (F10) |
| FR-012 | A degraded (partially failed) investigation is surfaced as degraded in the API and UI, never as complete | Silent partial results are worse than an explicit failure | Not started | §3.5 |
| FR-013 | All monetary columns are `NUMERIC(19,4)`; quantisation to currency minor units happens at presentation/settlement | Keeps precision decisions explicit and reversible | In progress (storage done and migration-tested; quantisation implemented as `pricing/rounding.py`, presentation not built) | §6.1, §6.2 |
| FR-014 | Audit history is append-only; no `UPDATE`/`DELETE` grants on `audit_events` for the application role | "Persist audit history" is meaningless if it is mutable | Not started | §11.2, §13 (INV-10) |
| FR-015 | Seed script loads a deterministic fictional Northstar Cloud dataset with at least one known-bad invoice | The assessment needs a reproducible scenario, and tests need known answers | Not started | §15 |
| FR-016 | Prompt text and prompt version are recorded on every investigation | Makes LLM output reproducible/attributable when behaviour changes | Not started | §7.2 |

## 6. Derived non-functional requirements (`DERIVED`)

| ID | Requirement | Rationale | Status |
| --- | --- | --- | --- |
| NFR-001 | Monetary arithmetic must be reproducible across runs and platforms | Decimal semantics must be pinned, not inherited from the platform | In progress (rounding mode pinned, `Money` value object) | §6.2 |
| NFR-002 | No floating-point arithmetic anywhere in the monetary path | Direct consequence of NEP-01; enforced by test + grep-able check | In progress |
| NFR-003 | LLM provider is swappable without changing domain or service code | Required by STK-06 | Not started |
| NFR-004 | Tests must not require network access or a running LLM | Deterministic mock provider is the default in tests | Not started |
| NFR-005 | The system must be runnable locally with one command | STK-05 | In progress (`docker compose up`) |
| NFR-006 | No secret may appear in a frontend bundle, log line, or API response | NEP-009 | In progress |
| NFR-007 | Untrusted customer text is treated as data, never as instructions | Prompt-injection defence | Not started |

---

## 7. Open questions blocking these requirements

These are tracked in full in `docs/ENGINEERING_DECISIONS.md` §"Open questions".

| OQ | Question | Blocks | Default assumption in the meantime |
| --- | --- | --- | --- |
| OQ-01 | Where does analyst/reviewer identity come from (SSO, internal auth, or mock)? | FR-006, NEP-04, STK-01 | Dev-mode header identity with two roles. **Explicitly demo-grade, not real auth.** |
| OQ-02 | Should adjustments be pre-tax or post-tax? | FR-010 | Adjustment applies to the invoice's pre-tax line subtotal; tax is not recomputed. |
| OQ-03 | Rounding mode: `ROUND_HALF_UP` (commercial) or `ROUND_HALF_EVEN` (banker's, IEEE)? | NFR-001 | `ROUND_HALF_UP` at invoice and line boundaries, `ROUND_HALF_EVEN` for residual allocation. **Implemented** in `pricing/rounding.py` as pinned constants and pinned by tests; still a business confirmation, since a different answer is a one-constant change plus its tests. |
| OQ-04 | Multi-currency and FX: in scope for v1? | FR-013 | Single currency per account; no FX; cross-currency adjustments rejected. |
| OQ-05 | May resolution options be non-monetary (e.g. "explain the charge", "correct contract metadata")? | FR-006 | Yes — `NO_ADJUSTMENT` and `REQUEST_INFO` exist alongside credit options. |
| OQ-06 | Retention/PII policy for dispute descriptions and snapshots? | NEP-006, NFR-006 | Retain indefinitely in dev; no PII/PCI stored beyond payment `last4`. |
| OQ-07 | Frontend test runner: Vitest + Testing Library, or Playwright only? | STK-04 | Vitest + Testing Library for components, Playwright deferred. |
| OQ-08 | Which real LLM provider adapter is needed beyond the mock? | STK-06, NEP-08 | Provider protocol + deterministic mock only. Real adapter added behind an env flag, not built speculatively. |
| OQ-09 | Expected data volume / latency budget for an investigation? | §14 | Not yet stated; current design targets a single-digit-second investigation for demo-scale data. |
| OQ-10 | Is `dispute.description` allowed to be arbitrarily long, and is there a size cap? | NFR-007 | Cap at 10,000 characters with a clear 422. |

### 7a. Open schema questions (raised by Phase 1 Slice 1)

These are not in the OQ list above because they were discovered while writing the first migration rather
than during the design phase. All three are recorded in `SYSTEM_DESIGN.md` §3.2 with the correction cost
for each.

| # | Question | Assumption in the meantime | Cost of being wrong |
| --- | --- | --- | --- |
| SQ-01 | What are the legal values of `contracts.status`? | `DRAFT`, `ACTIVE`, `SUPERSEDED`, `TERMINATED`, enforced by a generated `CHECK` | Low — widen one enum and one constraint |
| SQ-02 | What are the legal values of `contract_price_terms.billing_mode`? | `PER_UNIT`, `TIERED`, `COMMITMENT`, matching the calculators named in SYSTEM_DESIGN §6.3 | Low — same as SQ-01 |
| SQ-03 | Are contract `external_id`s unique globally or only within an account? | Unique globally, matching `accounts` | Medium — needs `UNIQUE (account_id, external_id)` and a migration if per-account |

SQ-01 and SQ-02 are stored as `CHECK`-constrained strings rather than PostgreSQL enum types specifically so
that being wrong is cheap. No document in the repository enumerates either vocabulary, so these are
assumptions and are labelled as such in the code that implements them.

---

## 8. Coverage summary

| Group | Total | Implemented + tested | Not started |
| --- | --- | --- | --- |
| Product story (PST) | 8 | 1 | 7 |
| Non-negotiable principles (NEP) | 10 | 1 | 7 (2 partial) |
| Stack (STK) | 6 | 1 | 2 (3 partial) |
| Documentation (DOC) | 8 | 8 | 0 |
| Derived functional (FR) | 16 | 1 | 13 (2 partial) |
| Derived non-functional (NFR) | 7 | 0 | 4 (3 partial) |

STK-03 (SQLAlchemy 2.0 + Alembic) moved to implemented in Phase 1 Slice 1: three tables, a reversible
migration, and 62 integration tests asserting the schema. Those tests are written and **have not been
executed against a live server yet** — the database role did not exist when the code was written. See
`AGENT_USAGE.md` §9.

**Honest read:** two slices are genuinely done — the database foundation and the deterministic billing
engine (ADR-022) — and everything between the evidence bundle and the model is not built. `app/api/`
exposes only the health endpoint; there is no LLM code, no evidence pipeline, and no review or
adjustment service.

Two claims are deliberately *not* upgraded. The 62 database integration tests are written and have
**still never executed against a live PostgreSQL server** in this checkout (no database is reachable),
so STK-03 remains unverified against the real engine. And NEP-02's stronger half — that the LLM response
schema contains no money fields — cannot be demonstrated at all until that schema exists; today it is
vacuously true because there is no LLM output to inspect. A schema that has been declared and
unit-tested is not the same as a schema PostgreSQL has accepted, and this repository does not claim the
latter.
