# Engineering Decisions — ResolveIQ

An ADR-style log. Each entry: **Context → Decision → Consequences → Alternatives rejected → Revisit if**.

Status legend: `Accepted` · `Proposed` (needs review) · `Open` (undecided)

Nothing here is a decision I did not actually make. Where the brief was silent, the entry says so.

| ID | Title | Status |
| --- | --- | --- |
| ADR-001 | Exact decimal arithmetic in a dedicated `Money` value object | Accepted |
| ADR-002 | Hexagonal architecture over layered/clean architecture | Accepted |
| ADR-003 | The LLM response schema contains no monetary fields | Accepted |
| ADR-004 | The LLM selects a hypothesis code; a registry computes the money | Accepted |
| ADR-005 | Citations are structurally validated against an allowlist, with hard failure | Accepted |
| ADR-006 | Immutable evidence snapshots + content fingerprint | Accepted |
| ADR-007 | Five independent layers for duplicate-adjustment prevention | Accepted |
| ADR-008 | Per-stage investigation state machine with resume, and an explicit DEGRADED status | Accepted |
| ADR-009 | Injectable `LlmProvider` with a deterministic mock as the default | Accepted |
| ADR-010 | Append-only audit log enforced by database grants | Accepted |
| ADR-011 | Money stored as `NUMERIC(19,4)`, quantised only at presentation | Accepted |
| ADR-012 | Monorepo with independent `backend/` and `frontend/` | Accepted |
| ADR-013 | Pydantic-validated structured output with one repair retry, then degrade | Proposed |
| ADR-014 | SQLAlchemy 2.0 typed `Mapped[]` models, Alembic for all schema change | Accepted |
| ADR-015 | Deviations from the preferred stack, and why | Accepted |
| ADR-016 | Synchronous investigation; no job queue yet | Proposed |
| ADR-017 | Evidence keys are human-readable strings, not database IDs | Accepted |
| OQ-01…OQ-10 | Open questions | Open |

---

## ADR-001 — Exact decimal arithmetic in a dedicated `Money` value object

**Status:** Accepted

**Context.** NEP-01 forbids binary floating point for money. The trap is that `Decimal` alone is not
enough: raw `Decimal` arithmetic has no currency, permits `Decimal("NaN")`, and invites implicit
rounding via the ambient decimal context.

**Decision.** A frozen `Money` dataclass (`amount: Decimal`, `currency: str`) is the only type permitted
to hold an amount. It rejects `float` outright, rejects non-finite values, validates ISO-4217-shaped
currency codes, refuses cross-currency arithmetic, and requires an explicit rounding mode for any
operation that rounds. Storage is `NUMERIC(19,4)`.

**Consequences.**
- Float cannot enter the money path without an immediate, loud `TypeError`. This is the whole point: a
  silent `0.1 + 0.2 != 0.3` bug in a billing system is expensive to find later.
- Every money signature in the system is visibly `Money`, so reviewers do not have to ask "is this exact?".
- Cost: verbosity (`m1.amount + m2.amount` rather than `+`), and JSON serialisation needs an explicit
  encoder. Accepted: clarity beats terseness in financial code.
- `Money` is deliberately *not* a "smart" money type — no FX, no tax, no allocation. Those are separate
  concerns that would violate SRP.

**Alternatives rejected.**
- *Bare `Decimal` everywhere* — no currency, ambient context leaks in, and `Decimal` has no place to
  enforce the float ban on construction of API payloads.
- *`int` minor units* — exact and fast, but sub-cent unit prices (e.g. $0.0007/API call) need 4 dp, and
  mixing scales is its own bug source.
- *`float` with rounding at the end* — directly forbidden by NEP-01.

**Revisit if.** Multi-currency support with FX is confirmed (OQ-04) — then a `Money` + explicit
`ExchangeRate` pair, still no implicit conversion.

---

## ADR-002 — Hexagonal (ports & adapters) over layered or clean architecture

**Status:** Accepted

**Context.** The brief asks for simple architecture that is nonetheless testable and maintainable, and
lists hexagonal-adjacent structure nowhere. Three candidates: (a) classic layered (`router → service →
repository`), (b) hexagonal with explicit ports, (c) full clean architecture with use-case interactors
and domain entities.

**Decision.** Hexagonal. `domain/` and `pricing/` are pure Python with zero framework imports. I/O
lives behind `ports/` protocols, implemented in `adapters/`. One composition root (`api/deps.py`).

**Consequences.**
- The pricing engine and state machines are unit-testable with no database, no container, no mocking
  library. That is the single biggest testing dividend.
- The dependency rule is mechanically checkable (a pytest test that walks imports) rather than a
  convention people drift from.
- Cost: one extra layer of indirection and slightly more files for the same logic. For a codebase this
  size the indirection is justified *only* because the alternative (LLM + DB + HTTP in one layer) makes
  the money engine untestable in isolation.

**Alternatives rejected.**
- *Classic layered* — fewer files, but business rules end up in services that also manage sessions, so
  the money tests need a database. Directly harms NEP-01 verification.
- *Full clean architecture* — use-case interactors and entity factories would be indistinguishable from
  the services layer here, adding ceremony with no benefit at this scale. Violates NEP-10.
- *Microservices* — one team, one assessment, one database. Pure cost.

**Revisit if.** The domain grows to multiple bounded contexts with genuinely different invariants, or the
ingest pipeline needs independent deployment.

---

## ADR-003 — The LLM response schema contains no monetary fields

**Status:** Accepted

**Context.** NEP-02 requires financial calculation to be separate from AI interpretation. LLMs are
non-deterministic arithmetic engines; a model asked "how much was overcharged?" will produce a confident
number. Even a correct-looking one is unauditable and unreproducible.

**Decision.** `InterpretationResponse` (Pydantic, `extra="forbid"`) has **no field capable of holding a
monetary amount**. The model returns codes, narratives, confidence, and evidence citations. Amounts come
only from `pricing/`.

**Consequences.**
- Separation is enforced by the *parser*, not by prompt wording. A model that emits an amount fails
  validation — which is the intended, observable behaviour.
- The narrative can still contain a number in prose ("about $400 over"). Mitigation: the UI renders
  impact exclusively from `computed_impact` + `calculation_trace`, and narratives are labelled as
  model-authored text. Not perfectly airtight; see OQ in ADR-013 about numeric-claim linting.
- Requires a repair-retry path, since `extra="forbid"` turns a stray key into a hard error (ADR-013).

**Alternatives rejected.**
- *Prompt-only instruction not to return amounts* — advice, not a type. The brief's requirement deserves
  a structural guarantee.
- *Accept amounts and overwrite them with engine values* — leaves hallucinated numbers in the narrative
  where the human will read them.
- *Return amounts as strings inside a `notes` field* — the same problem with extra steps.

**Revisit if.** Never. This is the load-bearing decision for NEP-02.

---

## ADR-004 — The LLM selects a hypothesis code; a registry computes the money

**Status:** Accepted

**Context.** "Keep calculation separate from interpretation" needs a *seam*: something the model decides
and something deterministic code decides, meeting at a typed boundary. Passing whole calculations to the
model defeats the purpose; passing whole classifications to code requires the model to be unnecessary.

**Decision.** The model emits `hypothesis_code` from a **closed vocabulary**. `pricing/registry.py` maps
each code to a pure calculator. Unknown code ⇒ `UNEXPLAINED` with `impact = None`.

**Consequences.**
- The model contributes judgement (which explanation fits) while arithmetic stays reproducible and
  independently testable against golden fixtures.
- Adding a pricing rule is additive: new calculator + registry entry. Existing code untouched (OCP).
- The closed vocabulary is a constraint on the model. It is intentional: an unrecognised code must
  surface as "needs human judgement", not be guessed at.
- `UNEXPLAINED` being a first-class outcome keeps the system honest. Forcing every investigation to
  produce an impact would be a lie by omission.

**Alternatives rejected.**
- *Model returns a structured formula/DSL* — inventive, but it effectively lets the model do the
  arithmetic and makes every formula unvalidated code.
- *Rules run first, model only narrates* — loses the ability to weigh contradictory evidence, which is
  the actual value of the LLM.
- *Model returns a number plus a formula* — reintroduces ADR-003's problem.

**Revisit if.** The hypothesis vocabulary proves too rigid for real disputes; then extend the vocabulary
explicitly, never loosen the typing.

---

## ADR-005 — Citations validated against an allowlist, with hard failure

**Status:** Accepted

**Context.** NEP-03: AI findings must reference supplied evidence and never fabricate identifiers. This
needs a hard guarantee, not a prompt request.

**Decision.** `EvidenceCitationValidator` checks the model response against the exact key set of the
case bundle. Four rules: every cited key must exist; every finding must cite ≥1 key; model-asserted
inference goes in a separate `unverified_claims` field; **one invalid key rejects the entire
response**.

**Consequences.**
- Fabricated identifiers cannot reach persistence or the UI.
- Unsupported statements are structurally separated from findings, so the UI *cannot* render them with
  the same visual weight without deliberately going out of its way to do so.
- Hard failure costs a full retry cycle on a single bad citation. This is a deliberate trade: a silently
  filtered citation set is how a hallucination hides inside an otherwise-plausible answer.

**Alternatives rejected.**
- *Drop unknown keys silently* — hides exactly the failure we are trying to surface.
- *Downgrade to a warning and keep the finding* — then "cited evidence" is not a guarantee.
- *Post-hoc verification that each citation is semantically relevant* — requires a model judgement about
  the model's own output. Key-level validation is the honest, verifiable boundary; semantic relevance
  is left to the human reviewer, which is the point of NEP-04.

**Revisit if.** Semantic citation checking becomes cheap and reliable enough to be worth adding as an
*additional* signal (never as a replacement).

---

## ADR-006 — Immutable evidence snapshots with a content fingerprint

**Status:** Accepted

**Context.** NEP-07 requires stale-investigation detection and support for additional evidence. Both need
a definition of "the evidence changed". Live-querying billing tables cannot support that, and it makes
past findings irreproducible.

**Decision.** Each source record is copied into `evidence_items.snapshot` (JSONB) with a
`content_hash` (SHA-256 of canonical JSON) and a stable `natural_key`. The investigation stores an
`evidence_fingerprint` = hash over sorted `(natural_key, content_hash)`.

**Consequences.**
- An investigation is reproducible: the exact bytes it reasoned about are stored.
- Staleness reduces to one string comparison.
- Reviewer approvals record the fingerprint they saw, so an approval becomes void when facts change
  (mechanical, not a policy reminder).
- Cost: duplicated data and a re-snapshot step when new evidence arrives. Accepted at this scale;
  a high-volume system would snapshot a manifest plus content-addressed blobs.

**Alternatives rejected.**
- *Live queries + timestamp range* — a row edited in place silently changes history.
- *Version the source tables* — more invasive, and still needs a cross-table fingerprint.
- *Fingerprint only, no snapshot* — detects change but cannot reproduce or justify the finding.

**Revisit if.** Evidence volume per dispute becomes large enough that full snapshots are impractical.

---

## ADR-007 — Five independent layers for duplicate-adjustment prevention

**Status:** Accepted

**Context.** NEP-05 explicitly forbids solving duplicate adjustments with a disabled frontend button. A
button is trivially bypassed by curl, two tabs, a retry, or two analysts.

**Decision.** Five layers, documented in `SYSTEM_DESIGN.md` §9.3: client idempotency key + unique index;
partial unique index on active adjustments per dispute; `SELECT … FOR UPDATE` row lock on the dispute;
optimistic `version` column on `disputes`; state-machine guard on the transition.

**Consequences.**
- The database is the final arbiter. Correctness does not depend on any client behaving.
- Concurrency is *testable*: two threads, one barrier, assert exactly one `201` and one `409`. This is
  the test that actually proves NEP-05, and it is the first integration test in Phase 3.
- The idempotency key means a client retry after a timeout returns the original adjustment instead of an
  error the analyst cannot interpret.
- Cost: five mechanisms is more to understand. Mitigated by keeping each one small and single-purpose;
  a reviewer can verify each independently.

**Alternatives rejected.**
- *Frontend disable only* — forbidden by the brief, and wrong.
- *Check-then-insert without a unique index* — a race, not a control.
- *Advisory lock* — works, but a partial unique index states the rule declaratively and survives any
  other code path writing the table.
- *Serialisable isolation everywhere* — correct but broad and slow; row-level locking on the single
  contended row is narrower and sufficient.

**Revisit if.** Adjustments ever need to be split across invoices such that "one active per dispute" is
the wrong invariant.

---

## ADR-008 — Per-stage investigation state machine with resume, and an explicit DEGRADED status

**Status:** Accepted

**Context.** NEP-07 requires recoverable partial failures. A single "investigation succeeded/failed" flag
makes partial failure unrepresentable and forces all-or-nothing retries.

**Decision.** Five stages, each with its own persisted status. `COMPLETE`, `PARTIAL_FAILED`, and
`DEGRADED` are distinct terminal-ish states. Retries resume from the first non-completed stage.

**Consequences.**
- A crash after `INTERPRETING` does not discard collected evidence or repeat the LLM call.
- Because writes are per-stage and idempotent, resume is safe.
- `DEGRADED` (e.g. missing usage data) is distinguishable from `FAILED`, so the UI can say "incomplete
  analysis, 3 findings from available evidence" instead of a binary pass/fail. FR-012 exists specifically
  to stop degraded results being presented as clean ones.

**Alternatives rejected.**
- *All-or-nothing investigation transaction* — simplest, but one LLM hiccup loses everything and retry
  cost is maximal.
- *Single status field with a message* — cannot distinguish "no evidence" from "provider down", which are
  different analyst actions.
- *Queue with retries* — ADR-016; premature at this scale.

**Revisit if.** Investigation duration becomes user-visible (seconds → minutes).

---

## ADR-009 — Injectable `LlmProvider` with a deterministic mock as the default

**Status:** Accepted

**Context.** STK-06 requires an injectable provider interface and a deterministic mock. Tests must not
require network access.

**Decision.** `LlmProvider` Protocol (`structured_infer`) with `MockLlmProvider` (deterministic,
fixture/fingerprint driven) as the default in tests **and** in local dev. A real adapter would be added
later behind an env flag and must pass the same contract suite.

**Consequences.**
- Entire test suite runs offline and is reproducible; a model upgrade cannot break CI.
- Contract tests give a real provider a promotion criterion rather than "it seemed to work".
- The mock is honest about being a mock: it returns pre-authored, evidence-shaped interpretations, not
  clever guesses. A mock that "behaves like the model" would be a liability.

**Alternatives rejected.**
- *Recording/replaying HTTP fixtures* — brittle to library-internal changes, and the boundary we actually
  care about is the *structured response*, not the wire format.
- *Real provider in tests with recorded cassettes* — same brittleness plus cost.

**Revisit if.** OQ-08 names a real provider; the port already accommodates it.

---

## ADR-010 — Append-only audit log enforced by database grants

**Status:** Accepted

**Context.** NEP-06 requires persisted audit history. An audit table the application can `UPDATE` or
`DELETE` is decoration.

**Decision.** `audit_events` with no update path in the repository, plus a migration that `REVOKE`s
`UPDATE`/`DELETE` from the application role.

**Consequences.**
- Immutability is enforced by the database, not by developer discipline.
- Corrections are made by appending a superseding event, which is also the correct audit semantic.
- Requires a separate migration role vs. runtime role — a small operational cost, worth it.
- Caveat: a table owner can still mutate. True tamper-evidence needs hashing/chaining or an external
  sink; noted as future work rather than claimed as solved.

**Alternatives rejected.**
- *ORM-level convention only* — unenforced.
- *Trigger preventing UPDATE/DELETE* — works, but a grant revocation is simpler and more legible.

**Revisit if.** Compliance demands tamper-evident (hash-chained) audit records.

---

## ADR-011 — `NUMERIC(19,4)` storage, quantisation only at presentation

**Status:** Accepted

**Context.** Sub-cent unit prices are normal in usage billing ($0.0007 per call). Rounding at storage
loses money; rounding everywhere is noise.

**Decision.** `NUMERIC(19,4)` in Postgres (never `FLOAT`, never `REAL`). Quantise to currency minor
units only at presentation and settlement boundaries via an explicit `Money.quantise()`.

**Consequences.**
- Aggregations over line items are exact.
- 4 dp is a *chosen* precision, documented and tested, not an accident of a column type.
- `NUMERIC` is slower than float. Irrelevant at this volume, and correctness wins regardless.

**Alternatives rejected.**
- *`NUMERIC(19,2)`* — cannot represent a per-unit price of 0.0007.
- *Integer micro-units* — reintroduces scale confusion (ADR-001).
- *Float columns* — forbidden.

**Revisit if.** A currency with 0 or 3 minor units becomes in scope (would need a per-currency exponent
table rather than a hard-coded 2).

---

## ADR-012 — Monorepo with independent `backend/` and `frontend/`

**Status:** Accepted

**Context.** One product, two stacks, one team.

**Decision.** Single repository, `backend/` and `frontend/` as independent packages with their own
dependency manifests, plus top-level `docs/`, `docker-compose.yml`, `.env.example`.

**Consequences.**
- Cross-stack changes (e.g. adding a finding field) are one commit and one review.
- Independent lockfiles and build cycles are preserved — no accidental coupling through a root manifest.
- Cost: monorepo-wide tooling (lint, CI) needs a small amount of orchestration. Accepted at this size.

**Alternatives rejected.**
- *Two repositories* — atomic cross-stack changes become two PRs and a coordination problem, for no
  isolation benefit here.
- *Single `package.json` for everything* — the frontend toolchain has no business owning Python deps.

**Revisit if.** A second service with an independent lifecycle appears.

---

## ADR-013 — Pydantic-validated structured output, one repair retry, then degrade

**Status:** Proposed — needs review

**Context.** NEP-08 requires validation of structured LLM output. Real providers may emit malformed JSON,
extra keys, or invented citations.

**Decision.** Constrain decoding to a JSON schema where the provider supports it; validate with Pydantic
(`extra="forbid"`); on failure, one repair retry with the validation errors appended; on second failure,
mark the investigation `DEGRADED` with an explicit reason. Never silently accept partial output.

**Consequences.**
- Validation is a type boundary, not string parsing — no `eval`, no regex JSON extraction.
- Two failures produce a visible degraded investigation rather than a plausible-looking wrong one.

**Open sub-question.** The model may still narrate a numeric claim in prose, which the schema cannot
constrain. Proposed mitigation: lint narrative text for currency-shaped tokens and flag them in the UI as
model-authored. I have **not** implemented this; it risks false positives on legitimate references like
"the $4,200 payment" — which is exactly the sort of thing a reviewer should read anyway. Flagged rather
than silently decided.

**Revisit if.** False positives from narrative linting make it unusable in practice.

---

## ADR-014 — SQLAlchemy 2.0 typed models, Alembic for all schema change

**Status:** Accepted

**Context.** STK-03. Schema drift between models and database is a common source of production incidents.

**Decision.** SQLAlchemy 2.0 `DeclarativeBase` with `Mapped[...]`/`mapped_column()` annotations. Alembic is
the only way the schema changes; no `create_all` outside tests. A CI check fails if the autogenerated
migration is empty while models differ.

**Consequences.**
- Types are checked by the type checker, including `NUMERIC` columns mapped to `Decimal`.
- Explicit migrations mean the partial unique index of ADR-007 is versioned and reviewable — which
  matters, because that index *is* the duplicate-prevention guarantee.

**Alternatives rejected.**
- *Alembic autogenerate without review* — autogenerate misses server defaults, partial indexes, and
  data migrations.
- *`metadata.create_all`* — unversioned, unreviewable, and silent about destructive changes.

**Revisit if.** Never. This is table stakes.

---

## ADR-015 — Deviations from the preferred stack

**Status:** Accepted

Full disclosure of where I did not follow the preferred stack exactly.

| Preferred | Chosen / deviation | Reason |
| --- | --- | --- |
| React, TS, Vite, Tailwind, shadcn/ui | Followed exactly | — |
| Python, FastAPI, Pydantic | Followed (Pydantic v2) | — |
| PostgreSQL, SQLAlchemy, Alembic | Followed exactly | — |
| pytest + "appropriate frontend tests" | Vitest + Testing Library proposed; Playwright e2e deferred | OQ-07: "appropriate" was not specified. Deferred rather than guessed. |
| Docker Compose for local dev | Followed, plus native (non-Docker) path documented | Docker Desktop was not verified in this environment, so a native path is documented for review. |
| Injectable LLM provider + deterministic mock | Followed exactly | — |
| — | No ORM-adjacent CRUD generator, no `repository` base class | NEP-10: less magic, more readable code for a reviewer learning the codebase. |
| — | No CI pipeline yet | Out of scope for the foundation; noted in README as a Phase 5 item. |

---

## ADR-016 — Synchronous investigation; no job queue

**Status:** Proposed — needs review

**Context.** An investigation involves several DB round-trips plus one (possibly slow) LLM call. That is
queue-shaped work.

**Decision.** Run it synchronously for now. Add a worker only if measured latency is unacceptable.

**Consequences.**
- No Redis/Celery/RQ dependency, no job state to reconcile, failures surface as HTTP errors, and the
  whole flow is debuggable in one process.
- Risk: a slow provider holds an HTTP connection. Mitigated by a bounded timeout and a `PARTIAL_FAILED`
  path that is resumable.

**Alternatives rejected.**
- *Celery + Redis from the start* — two more services to run locally, for a flow measured in seconds.
  Classic premature infrastructure.
- *FastAPI `BackgroundTasks`* — loses the ability to report stage status or resume after a process
  restart, which is precisely what NEP-07 asks for.

**Revisit if.** A real provider makes the flow exceed a few seconds, or investigation becomes
user-triggered-and-fire-and-forget.

---

## ADR-017 — Evidence keys are human-readable strings, not database IDs

**Status:** Accepted

**Context.** Findings must cite evidence. Citing `evidence_item.id` (a bigint or UUID) is compact but
meaningless to a human reading a ticket or a screenshot.

**Decision.** Citations use `natural_key` strings such as
`invoice:INV-2026-03-0042#line:LI-0007`.

**Consequences.**
- A citation is self-describing in the UI, in a review note, and in an audit record.
- The allowlist in ADR-005 is a set of strings — trivially comparable and diffable.
- Cost: keys are wider than IDs and must be generated consistently. Mitigated by one constructor
  function per evidence type, unit-tested.

**Alternatives rejected.**
- *Surrogate IDs* — unreadable in exactly the context where readability matters most.
- *Natural primary keys in source tables* — couples our schema to theirs.

**Revisit if.** Never; the readability benefit dominates.

---

## Open questions

Tracked in full in `REQUIREMENTS_TRACEABILITY.md` §7. Condensed here with the decision each one blocks.

| OQ | Question | Blocks | Default assumed | Cost of being wrong |
| --- | --- | --- | --- | --- |
| OQ-01 | Identity source for analyst/reviewer roles | Auth, NEP-04 | Dev-mode headers, explicitly demo-grade | **High** — presenting headers as "auth" would be a false security claim |
| OQ-02 | Are adjustments pre-tax or post-tax? | FR-010, impact calculator | Pre-tax line subtotal; tax not recomputed | High — wrong tax handling is a real-money bug |
| OQ-03 | Rounding convention: `ROUND_HALF_UP` vs `ROUND_HALF_EVEN`? | Every computed amount | `HALF_UP` at invoice boundaries, `HALF_EVEN` for residual splits | Medium — off-by-a-cent findings against the real billing system |
| OQ-04 | Multi-currency / FX in scope? | `Money`, ADR-001 | Single currency per account; cross-currency rejected | Medium |
| OQ-05 | Are non-monetary resolutions allowed? | FR-006 | Yes — `NO_ADJUSTMENT`, `REQUEST_MORE_INFO` | Low |
| OQ-06 | Retention and PII policy for dispute text and snapshots | NEP-006, NFR-006 | Indefinite retention in dev; no PCI stored | Medium — a compliance finding |
| OQ-07 | Frontend test runner choice | STK-04 | Vitest + Testing Library; Playwright deferred | Low |
| OQ-08 | Which real LLM provider adapter is needed? | STK-06 | Port + mock only; no speculative adapter | Low |
| OQ-09 | Volume and latency targets | ADR-016 | Demo scale; seconds, not minutes | Medium — would force the job queue |
| OQ-10 | Dispute description size cap | NFR-007 | 10,000 chars, 422 above that | Low |

**Highest-risk assumption:** OQ-01. If a reviewer reads dev-mode header auth as "authentication
implemented", that is a misrepresentation. It is called out in `SYSTEM_DESIGN.md` §11.2, in the README,
and here for exactly that reason.
