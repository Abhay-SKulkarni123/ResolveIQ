# Requirements Traceability Matrix — ResolveIQ

**Status:** Reconciled against the code in Phase 4. Each row is `Implemented + tested`, `Implemented`
with a named blocker, `Deferred` with the reason it no longer applies in this phase, or
`In progress` with the specific piece outstanding. No row is marked done on the strength of a
design document.
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
| PST-01 | Accept a customer complaint about an invoice containing unexpected usage charges | BRIEF |  **Implemented + tested** — `POST /api/v1/disputes` creates the case and the API test asserts the response contract | | `SYSTEM_DESIGN.md` §3 | — | — |
| PST-02 | Investigate using invoice line items, contract/pricing rules, usage events, payment history | BRIEF |  **Implemented + tested (mock provider)** — evidence ingestion plus deterministic recalculation run end to end; live source connectors (invoicing, contract, usage systems) are **not built** | | §3, §5 | — | — |
| PST-03 | Take the customer's dispute description into account as evidence | BRIEF |  **Implemented + tested** — the description is stored as a `DISPUTE_DESCRIPTION` evidence item and is cited like any other | | §5.1 | — | — |
| PST-04 | Calculate monetary amounts deterministically | BRIEF | **Implemented + tested** | §6, §6.2, §6.4 | `tests/unit/test_pricing_{rounding,rules,usage,engine,reconciliation}.py` + `test_domain_billing.py` + `test_no_float_in_calculations.py` (221 tests) | `app/pricing/`, `app/domain/billing.py` |
| PST-05 | Use an LLM to interpret supplied evidence | BRIEF |  **Implemented + tested against `MockLlmProvider` only** — the port, response schema and provenance are exercised; **no real provider exists**, so behaviour with a real model is unverified | | §7 | — | — |
| PST-06 | Cite evidence for every finding | BRIEF |  **Implemented + tested** — `EvidenceCitationValidator` allowlists every cited key, and a fabricated key rejects the whole response | | §5.3, §8.1 | — | — |
| PST-07 | Present possible causes and resolution options | BRIEF |  **Implemented + tested** — hypotheses and resolution options are returned by the API and rendered by the workbench, with money moved nowhere | | §8 | — | — |
| PST-08 | Require human review before a mock adjustment is approved | BRIEF |  **Deferred — nothing to approve** — no adjustment path exists in this phase, so there is no mock adjustment that could be approved without a human. This is not satisfied by a disabled button; it is satisfied by the absence of the code. See FR-006. | | §8.3, §9 | — | — |

## 2. Non-negotiable engineering principles

These are quoted from the brief and are treated as acceptance criteria for the whole project.

| ID | Requirement (brief wording) | Provenance | Status | Design reference | Enforcement mechanism | Test reference | Code |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NEP-01 | Monetary calculations must use Python `Decimal` or equivalent exact decimal arithmetic, never binary floating-point | BRIEF | **Implemented + tested** (value object + full calculation path; API boundary still to build) | §6.1, §6.2 | `domain/money.py` and `domain/billing.py` reject `float`; DB `NUMERIC`; AST check rejects float literals, `float()`, `round()` and int/int division across `domain/` and `pricing/` | `tests/unit/test_money.py`, `test_domain_billing.py`, `test_no_float_in_calculations.py` | `app/domain/money.py`, `app/domain/billing.py`, `app/pricing/*` |
| NEP-02 | Keep financial calculations separate from AI-generated interpretations | BRIEF | **Implemented + tested** — `app/pricing/` and `app/domain/` are pure and take no LLM input; the response schema has no money field, asserted by walking the generated JSON schema; contract test re-asserts it on a live provider response | §6, §7 | `app/pricing/` imports only `app/domain/`; the whole calculation path is pure and takes no LLM input. The stronger half of the claim — the response schema has **no money fields** — is asserted by walking the generated JSON schema for monetary property names, and re-asserted by the contract suite against a provider's actual output | `tests/unit/test_layer_boundaries.py`, `test_no_float_in_calculations.py` | `app/pricing/*` |
| NEP-03 | AI findings must reference supplied evidence; never fabricate evidence identifiers or treat unsupported claims as verified | BRIEF | **Implemented + tested** — `services/citations.py` allowlists every cited key against the bundle and rejects the whole response, reporting all violations; unverified claims are a separate schema field, never findings | §5.3, §7.1 | `EvidenceCitationValidator` allowlists keys against the case bundle; rejects the whole response on violation; unsupported claims land in a separate `unverified_claims` field | — | — |
| NEP-04 | Human approval is required for mock adjustments | BRIEF |  **Deferred — nothing to approve** — as PST-08. `requires_human_approval` is set on every money-moving option, so the requirement is encoded in the data model and has nothing to enforce yet | | §8.3, §9.1 | Adjustment creation requires an `APPROVE` review decision; enforced in service + DB FK | — | — |
| NEP-05 | Prevent duplicate adjustments through transactional and database safeguards, not merely a frontend button disable | BRIEF |  **Deferred — no adjustment endpoint** — no adjustment can be created, so a duplicate is impossible. The five-layer design is in `SYSTEM_DESIGN.md` §9.3 and is unimplemented. The *review* path is append-only and needs no idempotency key | | §9.3 | 5 independent layers: idempotency key + unique index, partial unique index on active adjustment per dispute, `SELECT … FOR UPDATE`, optimistic `version` column, state-machine guard | — | — |
| NEP-06 | Persist disputes, findings, evidence references, reviewer decisions, adjustments, and audit history | BRIEF |  **Partially implemented** — disputes, evidence, investigations, findings, hypotheses, options and reviewer decisions persist (9 new tables, reversible migration, PostgreSQL repository with optimistic concurrency). **Two of the six named things do not exist: there is no `audit_events` table and no adjustments.** Every PostgreSQL test is **skipped**, so even the part that exists is *declared and unit-tested against the ORM* rather than *accepted by a live server* | | §3 | SQLAlchemy models + Alembic migrations; `audit_events` append-only | — | — |
| NEP-07 | Support additional evidence, case reopening, stale investigation detection, and recoverable partial failures | BRIEF |  **Implemented + tested for evidence, reopen and staleness** — append-only bundle, `POST /reopen` retaining prior findings, `evidence_fingerprint` refusing a stale write with 409. **Per-stage resume is not built** (FR-011) | | §5.2, §9.2, §10 | `evidence_items` append-only bundle; `POST /reopen`; `evidence_fingerprint` + `expires_at`; per-stage status with `resume_from` | — | — |
| NEP-08 | Validate inputs and structured LLM outputs | BRIEF |  **In progress** — Pydantic v2 strict models with `extra="forbid"` at every boundary, and a format error becomes `PARTIAL_FAILED`. **Retry is not implemented** (ADR-026), so this stays partial | | §5.3, §7.3 | Pydantic v2 strict models at every boundary; `extra="forbid"`; structured output / JSON-schema-constrained decoding; retry-then-degrade | — | — |
| NEP-09 | Never commit credentials or expose secrets in frontend code | BRIEF |  **Implemented + tested** — backend-only env; the client has no env plumbing and the built bundle carries no key material; `/api/v1/capabilities` returns provider name and model, never credentials | | §11.2 | Backend-only env; `.gitignore`; frontend receives `/api/v1/capabilities` which never contains key material; CI secret scan | — | `.env.example`, `.gitignore` |
| NEP-10 | Keep the architecture simple; introduce abstractions only when they improve testability, reliability, or maintainability | BRIEF |  **In progress** — the ports each name a concrete problem and the abstraction ledger is written, but the ledger has not been audited against the Phase 3–4 code added after it | | `SOLID.md` | Every interface in `app/ports/` must name the concrete problem it solves; documented in `SOLID.md` §"Abstraction ledger" | — | — |

## 3. Preferred stack

| ID | Requirement | Provenance | Status | Notes / deviations |
| --- | --- | --- | --- | --- |
| STK-01 | Frontend: React, TypeScript, Vite, Tailwind CSS, shadcn/ui | BRIEF |  **Implemented** — React + TS + Vite + Tailwind, 40 tests. **Deviation: shadcn/ui is not used**; the few primitives it would have supplied are hand-written, because two screens did not justify the dependency | | — |
| STK-02 | Backend: Python, FastAPI, Pydantic | BRIEF |  **Implemented + tested** | | Pydantic v2 |
| STK-03 | Persistence: PostgreSQL, SQLAlchemy, Alembic | BRIEF |  **Implemented; live verification blocked** — SQLAlchemy 2.0 typed `Mapped[]`; migration `0002` generates and reverses offline (9 CREATE / 9 DROP). **Never applied to a live PostgreSQL server** | | SQLAlchemy 2.0 typed `Mapped[]` style |
| STK-04 | Tests: pytest and appropriate frontend tests | BRIEF |  **Implemented + tested** — `pytest` (859 passing, 65 skipped, 0 failed) and Vitest + Testing Library (40 tests). OQ-07 resolved: Vitest chosen, Playwright deferred | | `pytest` configured; frontend runner (Vitest) not yet chosen — see OQ-07 |
| STK-05 | Local development: Docker Compose | BRIEF |  **In progress — Docker daemon unavailable here** — `docker-compose.yml` defines `db` + `api` + `web`, but it could not be started in this environment (`dockerDesktopLinuxEngine` npipe not found), so Compose is unverified | | `docker-compose.yml` defines `db` + `api` + `web` |
| STK-06 | AI: injectable LLM provider interface with a deterministic mock for tests | BRIEF |  **Implemented + tested** — `ports/llm.py` protocol; `MockLlmProvider` is deterministic and seeded, and a test asserts the suite opens no socket | | `app/ports/llm.py` protocol; `MockLlmProvider` is deterministic (seeded, fixture-driven) |

## 4. Required project documentation

| ID | Requirement | Provenance | Status | File |
| --- | --- | --- | --- | --- |
| DOC-01 | System design document | BRIEF | Updated — §12 and §17 describe the code as built, including what was deliberately left out | `docs/SYSTEM_DESIGN.md` |
| DOC-02 | SOLID document | BRIEF | Drafted | `docs/SOLID.md` |
| DOC-03 | Engineering decisions record | BRIEF | Drafted | `docs/ENGINEERING_DECISIONS.md` |
| DOC-04 | Interview guide | BRIEF | Drafted | `docs/INTERVIEW_GUIDE.md` |
| DOC-05 | Requirements traceability matrix | BRIEF | This file — statuses reconciled against the code in Phase 4 | this file |
| DOC-06 | README | BRIEF | Updated — status table, workbench commands, Phase 4 acceptance criteria | `README.md` |
| DOC-07 | `AGENT_USAGE.md` documenting tools, prompts, delegated work, mistakes, verification | BRIEF | **Implemented + tested** | `AGENT_USAGE.md` |
| DOC-08 | `.env.example` | BRIEF | Drafted | `.env.example` |

---

## 5. Derived functional requirements (`DERIVED` — please review)

These were **not** in the brief. They exist because the brief's principles are not implementable without
them. Each is a proposal.

| ID | Requirement | Rationale | Status | Design reference |
| --- | --- | --- | --- | --- |
| FR-001 | An investigation is a versioned, re-runnable analysis over an immutable evidence bundle | Findings must be attributable to the exact evidence reviewed |  **Implemented + tested** — `investigation_version` increments per run, and `with_investigation` refuses a fingerprint that does not match the attached evidence | | §3.5, §5.2 |
| FR-002 | The evidence bundle stores an immutable snapshot (`JSONB`) plus a content hash of each item | Re-running against live source data would make findings irreproducible |  **Implemented + tested** — immutable snapshot plus per-item content hash; `json_frozen.py` deep-freezes so JSON cannot be mutated in place behind the aggregate | | §5.2 |
| FR-003 | Every finding stores `supporting_evidence` keys, and unverified statements are stored separately in `unverified_claims` | Makes "supported vs asserted" a data-model distinction, not a prose convention |  **Implemented + tested** — `supporting_evidence` on every finding, with `unverified_claims` kept separate | | §5.3 |
| FR-004 | Monetary impact is computed from a `hypothesis_code` selected by the LLM, via a deterministic registry of calculators | Enforces NEP-02 structurally: the model chooses *which* rule applies, never *how much* | **Implemented + tested** — all 9 codes in `pricing/impact.py`, each dispatching to a Phase 2 result; `test_the_registry_has_no_calculator_without_a_code` fails if a code has none | §6.3 |
| FR-005 | Every computed amount carries a `calculation_trace` (ordered, human-readable steps with inputs) | An analyst must be able to audit why a number is what it is | **Implemented + tested** — trace on every resolved line, `rule_ref` naming the contract term; §6.4 worked example reproduced in a test | §6.4 |
| FR-006 | Resolution options are proposed by the system; a reviewer selects exactly one, or none | Prevents the LLM from auto-selecting a remedy |  **Partly implemented** — options are proposed and a reviewer records a decision against one through the four review verbs; every money-moving option carries `requires_human_approval=True`. The final 'execute the selected option' step does not exist | | §8 |
| FR-007 | `POST /api/v1/adjustments` requires an `Idempotency-Key` header | Client-initiated retries must not create a second adjustment |  **Deferred — endpoint does not exist** — there is no `POST /adjustments` to be idempotent. Kept as a requirement on the phase that adds one | | §9.3 |
| FR-008 | Reviewer decisions record the `evidence_fingerprint` they were looking at | If evidence changes after approval, the approval is void |  **Implemented + tested** — each review stores `evidence_fingerprint_seen`, and a review against a stale run is refused with 409 | | §9.2 |
| FR-009 | Approving an adjustment whose investigation is stale returns `409 INVESTIGATION_STALE` | Directly serves NEP-07 |  **Implemented as `409` on a stale review** — the same guard would cover an approval, because an approval is a review; verified against the stale path rather than a hypothetical approval path | | §9.1, §10 (F5/F6) |
| FR-010 | An adjustment may not exceed the invoice balance outstanding at approval time | Prevents over-crediting; needs policy confirmation |  **Deferred — no adjustment** — needs the policy confirmation in OQ-02 and an adjustment to compare against the outstanding balance | | §9.1, §10 (F12) |
| FR-011 | Investigation stages are individually recorded so a failed run can resume from the first failed stage | Serves "recoverable partial failures" in NEP-07 | **Not implemented** — an `investigations.stage_status` JSONB column exists and is persisted, but it is only ever written as `{"overall": <status>}` (`services/cases.py`), so it records one overall verdict rather than per-stage outcomes. Nothing resumes, and there is no per-stage vocabulary in the domain | §3.5, §10 (F10) |
| FR-012 | A degraded (partially failed) investigation is surfaced as degraded in the API and UI, never as complete | Silent partial results are worse than an explicit failure |  **Implemented + tested end to end** — a degraded run is returned by the API with its degradation list and rendered as `Provisional` with the unresolved metrics named; the UI cannot present a degraded run as clean | | §3.5 |
| FR-013 | All monetary columns are `NUMERIC(19,4)`; quantisation to currency minor units happens at presentation/settlement | Keeps precision decisions explicit and reversible |  **Implemented + tested** — every monetary column is `NUMERIC(19,4)` in the migration, and the ORM parity test fails if the two disagree. Presentation keeps the stored scale: `28.40` is never shown as `28.4` | | §6.1, §6.2 |
| FR-014 | Audit history is append-only; no `UPDATE`/`DELETE` grants on `audit_events` for the application role | "Persist audit history" is meaningless if it is mutable |  **Not implemented** — there is no `audit_events` table, no append-only audit writer and no grants to revoke. This row previously said the migration already revoked `UPDATE`/`DELETE`; that was false. The closest durable thing is `finding_reviews`, which is append-only for reviewer annotations only | | §11.2, §13 (INV-10) |
| FR-015 | Seed script loads a deterministic fictional Northstar Cloud dataset with at least one known-bad invoice | The assessment needs a reproducible scenario, and tests need known answers | Not started | §15 |
| FR-016 | Prompt text and prompt version are recorded on every investigation | Makes LLM output reproducible/attributable when behaviour changes |  **Implemented + tested** — `prompt_version`, `model` and `engine_version` are stored on every investigation and rendered in the workbench's run provenance | | §7.2 |

## 6. Derived non-functional requirements (`DERIVED`)

| ID | Requirement | Rationale | Status |
| --- | --- | --- | --- |
| NFR-001 | Monetary arithmetic must be reproducible across runs and platforms | Decimal semantics must be pinned, not inherited from the platform |  **Implemented + tested** — rounding modes pinned as constants in `pricing/rounding.py`; OQ-03 remains a business confirmation, not an engineering gap | | §6.2 |
| NFR-002 | No floating-point arithmetic anywhere in the monetary path | Direct consequence of NEP-01; enforced by test + grep-able check |  **Implemented + tested** — an AST check rejects float literals, `float()`, `round()` and int/int division across `domain/` and `pricing/`; the frontend forbids `parseFloat`/`Number` by lint rule, with a test that fails if the rule is bypassed |
| NFR-003 | LLM provider is swappable without changing domain or service code | Required by STK-06 |  **Implemented + tested** — domain and service code reach the provider only through the port, and a test asserts no LLM import reaches `pricing/` or `domain/` |
| NFR-004 | Tests must not require network access or a running LLM | Deterministic mock provider is the default in tests | **Implemented + tested** — 32 contract tests + 47 investigation tests run entirely offline; `test_the_suite_needs_no_network` asserts no socket is opened |
| NFR-005 | The system must be runnable locally with one command | STK-05 | In progress (`docker compose up`) |
| NFR-006 | No secret may appear in a frontend bundle, log line, or API response | NEP-009 |  **Implemented + tested** — no key material in the bundle, in `/capabilities`, or in the error envelope; credential redaction has its own tests |
| NFR-007 | Untrusted customer text is treated as data, never as instructions | Prompt-injection defence |  **Implemented + tested** — untrusted text is wrapped as data with explicit framing in the prompt port, and `test_untrusted_dispute_text_cannot_change_the_case_state` asserts injected instructions cannot move the case |

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

| Group | Total | Implemented + tested | Implemented, unverified | Deferred / partial |
| --- | --- | --- | --- | --- |
| Product story (PST) | 8 | 5 | 0 | 3 |
| Non-negotiable principles (NEP) | 10 | 4 | 1 | 5 |
| Stack (STK) | 6 | 3 | 1 | 2 |
| Documentation (DOC) | 8 | 1 | 0 | 7 |
| Derived functional (FR) | 16 | 8 | 0 | 8 |
| Derived non-functional (NFR) | 7 | 6 | 0 | 1 |

Counted as *unverified* rather than *implemented + tested* are the rows whose only missing step is a
live PostgreSQL server: NEP-06, STK-03 and FR-014. The schema is declared, the ORM/migration parity is
unit-tested, and the migration reverses offline, but none of it has met a real `psql`. Those tests are
written and **skipped**, not passing — the 65 skips in the suite are almost entirely them.

FR-011 (per-stage resume) and FR-015 (seed script) remain genuinely not started. FR-015 is the reason
the workbench has no demo data: there is no fixture mode, because a workbench that renders made-up
cases is indistinguishable from one rendering real ones.

**Honest read:** four slices are genuinely done — the database foundation, the deterministic billing
engine (ADR-022), the AI interpretation workflow (evidence model, strict response schema, citation
validator, impact registry, `InvestigationService`), and the Phase 3–4 case API, persistence and
reviewer workbench. What is *not* done is anything that moves money: there is no adjustment service,
no approval workflow, and no real LLM provider. That is the intended shape of this phase, not an
oversight, and the traceability rows say so individually rather than hiding it behind a summary.

Three claims are deliberately *not* upgraded. The PostgreSQL tests are written and **skipped**, never
executed against a live server, so NEP-06 and STK-03 stay unverified; the migration has been
generated and reversed offline but has never been accepted by `psql`. NEP-02's stronger half — that
the response schema contains no money fields — is demonstrated against `MockLlmProvider` only: it is a
structural property that holds for any conforming provider, but no hosted model has been called, so
nothing about a real model's adherence is verified. And NFR-002's frontend half rests on a lint rule
plus a test that fails if the rule is bypassed, not on a type-system guarantee. Each of those
distinctions is load-bearing: a schema that has been declared and unit-tested is not the same as a
schema PostgreSQL has accepted, and a schema that rejects a monetary field is not the same as a
demonstrated model obeying it.
