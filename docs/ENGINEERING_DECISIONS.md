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
| ADR-018 | Deterministic constraint names in the SQLAlchemy `MetaData` | Accepted |
| ADR-019 | Surrogate UUID keys, and `RESTRICT` on every foreign key | Accepted |
| ADR-020 | The database checks the *shape* of money; the domain checks its *meaning* | Accepted |
| ADR-021 | Least-privilege database role for development and tests | Accepted |
| ADR-022 | Recalculation as pure functions over value objects; no invoice tables yet | Accepted |
| ADR-023 | The impact registry dispatches to the Phase 2 engine and computes nothing | Accepted |
| ADR-024 | `COMPLETE` is refused when the interpretation is missing or caveated | Accepted |
| ADR-025 | Confidence and likelihood cross the wire as strings, not JSON numbers | Accepted |
| ADR-026 | No retries in the provider port; `llm_max_retries` deliberately unread | Accepted |
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

## ADR-018 — Deterministic constraint names in the MetaData

**Status:** Accepted

**Context.** Alembic's autogenerate compares constraint *names* as well as shapes. A `CHECK`
constraint written without a name therefore compares unequal to itself across runs, so every
`alembic revision --autogenerate` proposes a migration that only renames it. Worse, the name
PostgreSQL actually stores is what a reviewer reads in an error message and what a `DROP CONSTRAINT`
needs.

**Decision.** `Base.metadata` carries a `naming_convention` dict, so every constraint name is derived
from the table and columns rather than typed out:

```
ix  -> ix_<table>_<columns>
uq  -> uq_<table>_<columns>
ck  -> ck_<table>_<explicit name>     # the name is required, and short
fk  -> fk_<table>_<column>_<referred table>
pk  -> pk_<table>
```

The `ck` entry interpolates an explicit name, so every `CheckConstraint` must pass `name=`. That is the
point: it forces a short readable name instead of one derived from the first hundred characters of a SQL
expression.

**The trap this creates, and the rule that follows.** Because the convention applies to migrations too,
a `CheckConstraint` written into a migration file must use the **bare** name. Passing the finished name
produces `ck_accounts_ck_accounts_external_id_not_blank`, and the longest ones come back from PostgreSQL
truncated to 63 characters with a hash suffix (`ck_contract_price_terms_ck_contract_price_terms_minimum_ab89`).
Both are silent: the migration succeeds and only the resulting schema is wrong. `alembic upgrade head
--sql` prints the offending names, and `tests/unit/test_persistence_models.py` asserts that every
constraint name fits the identifier limit.

**Consequences.**
- Autogenerate is stable and produces empty migrations when nothing changed.
- Tests can assert on exact constraint names, which is how `tests/integration/test_constraints.py`
  proves a rule is enforced rather than merely declared.
- A hand-written migration must repeat the bare names. This is duplicated on purpose: a migration that
  imported the model's constants would change meaning when the models change, and a migration must
  describe the schema as it was when it was written.

**Alternatives rejected.**
- *Name nothing* — unstable autogenerate, unreadable errors, unassertable constraints.
- *Let Alembic generate names from the expression* — truncated, hashed, and unstable across edits.

---

## ADR-019 — Surrogate UUID keys, and `RESTRICT` on every foreign key

**Status:** Accepted

**Context.** These tables mirror records owned by the customer's billing system, and an investigation
must be reproducible against the exact records it saw (FR-002). Two decisions follow from that, and
neither is obvious enough to leave unstated.

**Decision.**

1. Every table has a surrogate `uuid` primary key generated in Python by `uuid4`. The source system's
   identifier lives in a separate `external_id` column with a uniqueness constraint.
2. Every foreign key states `ondelete="RESTRICT"`, including where that is PostgreSQL's default.
3. No `updated_at` column. Source rows are append-only: a change of commercial terms produces a new
   effective-dated `contracts` row rather than an edit.

**Consequences.**
- A re-ingest that finds two rows claiming one source identifier has distinct rows to reconcile, rather
  than silently overwriting one with the other. Uniqueness on `external_id` then surfaces the conflict
  as a constraint violation instead of as missing data.
- Deleting an account that still has contracts is an error the caller must handle. A cascade would
  destroy the financial history a dispute is about as a side effect of removing a parent row, which is
  the worst possible failure mode for this domain.
- Python-side generation means the key is visible without a round trip and the schema carries no
  dependency on a particular PostgreSQL version's UUID function.
- Writing the default delete behaviour out costs three words per foreign key and makes the intent
  reviewable. A reader should not have to know what PostgreSQL does by default to know what this schema
  does.
- Without `updated_at` there is no mutable-row audit trail. That is acceptable because reproducibility
  rests on the immutable evidence snapshot (ADR-006), not on row history.

**Alternatives rejected.**
- *Source identifier as primary key* — couples our schema to theirs, and is the same mistake ADR-017
  rejected for evidence keys.
- *`bigserial`* — enumerates accounts, which is unnecessary information to leak and makes ids
  meaningful across databases in a way that invites cross-database joins.
- *`CASCADE`* — destroys billing history silently.
- *`updated_at`* — implies an editing workflow that does not exist.

---

## ADR-020 — The database checks the shape of money; the domain checks its meaning

**Status.** Accepted

**Context.** The `CHECK` constraints in `contract_price_terms` protect two different kinds of mistake,
and drawing the line between them decides where future billing logic lives.

**Decision.** The database enforces what is true of the *representation*, and nothing else:

- money is `NUMERIC(19,4)` and never a floating-point type (ADR-011)
- amounts and quantities are not negative
- currency is three uppercase letters
- `status` and `billing_mode` are members of the enums in `app/domain/contracts.py`
- `tier_schedule` is present exactly when `billing_mode = 'TIERED'`

The domain enforces what is true of the *business*: that a `COMMITMENT` term has a
`minimum_commitment`, that a tier ladder's thresholds ascend and do not overlap, that a term's currency
matches its account's, and that `effective_from` falls in the period the usage occurred.

**Consequences.**
- The `CHECK` value lists are generated from the Python enums, so the database and the domain cannot
  drift apart; a new enum member widens the constraint in the same commit.
- The internal shape of `tier_schedule` is *not* checked in SQL. Duplicating the tier semantics in a
  `CHECK` would create two definitions that drift, and the tier ladder is read only by
  `app/pricing/tiered.py`. A malformed ladder is therefore rejected by the pricing layer rather than by
  the database, which is an accepted and stated gap.
- The same rule keeps `contract_price_terms.currency` equal to `accounts.currency` out of SQL: a
  cross-table check needs a trigger, and triggers put billing logic in the storage layer.

**Alternatives rejected.**
- *A JSON-schema `CHECK` on `tier_schedule`* — duplicates the domain definition and still cannot express
  cross-row rules.
- *A PostgreSQL enum type* — changing the legal values means a type migration rather than a cheap
  constraint change, and the vocabulary is explicitly still under discussion (SYSTEM_DESIGN §3.2).

---

## ADR-021 — Least-privilege database role for development and tests

**Status.** Accepted

**Context.** ADR-010 relies on PostgreSQL grants to make the audit log append-only, so the shape of the
development role is a security decision and not just convenience. A superuser-prompted test suite can
defeat any grant-based control, because the role that runs it can always grant itself more.

**Decision.** Two databases, `resolveiq` and `resolveiq_test`, both owned by a single `resolveiq` role
that is `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`. `backend/scripts/create_dev_database.sql`
creates it idempotently and reports the resulting privileges.

**Consequences.**
- The role owns exactly the two databases the project needs, which is the minimum that lets
  `alembic upgrade head` and `alembic downgrade base` work: DDL on its own tables.
- It cannot create databases, create roles, or bypass row-level security, so a compromised test run
  cannot escalate.
- The destructive part of the migration test (`alembic downgrade base` drops every table) is confined to
  `resolveiq_test` by construction rather than by care. `Settings.test_database_url` derives the test
  database from the development one by swapping only the database name, so the two cannot drift apart in
  host or password, and a conftest fixture refuses to run if the database name is not `resolveiq_test`.
- The password lives in the gitignored `.env`. `Settings` reads `../.env` as well as `./.env` because
  every documented command runs from `backend/` while the repository keeps `.env` at its root; without
  that, the documented "put your credentials in `.env`" instruction silently did nothing.

**Alternatives rejected.**
- *Reusing a superuser for tests* — the usual arrangement, and the reason grant-based controls like
  ADR-010 are so often untested.
- *Docker Compose for the test database* — not available on every machine, and it would have left this
  schema unverified on a developer laptop.

---

## ADR-022 — Recalculation as pure functions over value objects, with no invoice tables yet

**Status.** Accepted

**Context.** The database foundation (ADR-018 to ADR-021) created `accounts`, `contracts` and
`contract_price_terms` and nothing else. Phase 2 needs to recalculate an invoice, which appears to
require `invoices`, `invoice_lines`, `usage_events`, `payments` and `adjustments`. Two facts argue
against building those tables now. ADR-006 already commits to immutable evidence snapshots, so a
recalculation is a pure function of a frozen JSONB bundle — it reads no live table. And the thing
worth testing is the arithmetic: a wrong rate or a wrong rounding rule is a real-money defect, and
it is fully reachable without a database.

Adding five tables now would also mean guessing at columns the evidence format does not yet settle —
whether an invoice is stored whole or per line, whether usage keeps its source-system identifiers.

**Decision.** No invoice, usage, payment or adjustment tables in this phase. The calculation inputs
are frozen dataclasses in `app/domain/billing.py`, validated at construction. The engine
(`app/pricing/engine.py`) is a pure function of `(invoice, price_terms, usage_events, contract_id)`.
Alongside that:

- **A discrepancy is a result, not a diagnosis.** `LineRecalculation` reports that 21.86 was
  recalculated against 28.40 recorded. It does not claim the cause. Deciding whether that is a
  duplicate, a wrong tier boundary or a stale contract version is the investigation's job, and a
  result with a cause attached is harder to challenge: the reviewer must disprove the diagnosis
  before trusting the arithmetic.
- **Three statuses, not two.** `RECALCULATED`, `ACCEPTED_AS_RECORDED` (a `FIXED` fee, which no usage
  evidence can confirm or refute) and `UNRESOLVED`. Collapsing the last two into "no discrepancy"
  would let a partially verified invoice read as a clean one.
- **An unresolved line makes the total a lower bound.** `InvoiceRecalculation.is_complete` is False
  whenever any line is unresolved, because such a line might have been wrong in either direction.
- **Recalculation is separate from reconciliation.** `reconcile_balance()` lives in its own module and
  takes payments and adjustments as arguments. A balance depends on events after the invoice, so
  merging the two would make the arithmetic unverifiable without also asserting something about
  account state.
- **The balance uses the recalculated total**, never the invoice's stated total. Using the stated
  total would make the two agree by construction.
- **Payment allocations are per invoice.** `PaymentAllocation` is its own type because one remittance
  routinely settles several invoices. A single `allocated` total cannot express a $300 payment split
  across two invoices, and inferring the split would either ignore the payment or over-apply it.
- **Adjustments are signed.** Positive is a credit, negative is a surcharge, so one formula —
  `total − payments − adjustments` — covers both directions and neither can be handled correctly
  while the other is handled backwards.
- **Usage timestamps must be timezone-aware and are normalised to UTC.** A naive timestamp is two
  different instants depending on who produced it, and a boundary event would then be billed in a
  different month by two systems that both believe they are right. Period selection compares the UTC
  calendar date, so an event at `2025-04-01T00:00+05:30` bills into March.
- **INV-01 is enforced by AST, not by review.** `tests/unit/test_no_float_in_calculations.py` walks
  `domain/` and `pricing/` and rejects float literals, `float()` calls, `round()`, and int/int
  division. The `float` argument of an `isinstance` check is exempt, structurally, because both
  `Money` and the billing inputs have to name it to reject it.

**Consequences.**
- The whole calculation path is covered by tests that need no database, and runs identically on any
  machine. This is what makes a dispute result defensible rather than merely reproducible.
- Nothing in this phase is wired to an API or a repository. `app/services/` and `app/api/` are
  untouched, which is the honest state of the work rather than a half-built vertical slice.
- Persistence is deferred, not designed around. When invoice storage is added, it will serialise
  these value objects into the evidence snapshot; the alternative would have been to design a schema
  now and migrate it once the evidence format settled.
- Adding an ORM entity for any of these types later is a real cost, and ADR-020's split — database
  checks shape, domain checks meaning — means the validation written here will not be discarded.

**Alternatives rejected.**
- *Creating `invoices`, `invoice_lines` and `usage_events` tables now* — five tables of guessed columns
  to support a calculation that needs none of them, on evidence that is stored whole anyway (ADR-006).
- *One `billing.py` doing recalculation and balance together* — the balance depends on post-invoice
  events, so a single function could not be checked against contract and usage alone.
- *Reporting an unresolved line as a zero discrepancy* — the most dangerous simplification available
  here, and the one a reader is least likely to notice.
- *Applying an unallocated payment to whichever invoice is open* — plausible, and it would silently
  convert an unapplied payment (§6.3 `UNAPPLIED_PAYMENT`) into a settled account.
- *Clamping a negative outstanding balance to zero* — it erases a refund that is genuinely due and
  makes the account look settled.
- *A filename allowlist for modules permitted to mention `float`* — the exemption is structural
  (the `float` argument of an `isinstance` call) because a list would need editing every time a new
  boundary check was added, and would silently stop applying to a renamed file.

---

---

## ADR-023 — The impact registry dispatches to the Phase 2 engine and computes nothing

**Status.** Accepted

**Context.** ADR-004 commits to the model selecting a `hypothesis_code` and a registry computing the
money. Phase 2 then built `app/pricing/` with tier, per-unit and commitment calculators, usage
deduplication, period filtering and balance reconciliation. §6.3 lists nine hypothesis codes, and
several of them describe things the engine already detects: `DUPLICATED_USAGE` is a `dedupe_key`
collision, `OUT_OF_PERIOD_USAGE` is a period filter, `UNAPPLIED_PAYMENT` is a reconciliation
question.

The obvious way to build the registry is to give each code its own calculator. That would put eight
or nine implementations of overlapping arithmetic in the repository, at least one of which would
disagree with Phase 2 about rounding or about which units count. In a dispute, "which of the two
calculators is right" is not a question anyone can answer under pressure.

**Decision.** `app/pricing/impact.py` maps each code to an assessor that reads what Phase 2 already
computed and reports it. It contains no pricing arithmetic of its own. Concretely:

- `OVERAGE_TIER_MISMATCH`, `UNIT_PRICE_MISMATCH` → the engine's `LineRecalculation.difference`, with
  its trace attached.
- `DUPLICATED_USAGE`, `OUT_OF_PERIOD_USAGE` → the same line difference, gated on
  `UsageSummary.duplicates` / `.out_of_period` actually being non-empty, with the anomaly counted in
  the basis string.
- `INCORRECT_ALLOCATION` → `OutstandingBalance.outstanding`.
- `STATEMENT_TOTAL_MISMATCH` → `stated_total - line_total`, read from the invoice, because this code is
  about the invoice disagreeing with itself rather than about whether the charge was right.
- `COMMITMENT_SHORTFALL` → the whole-invoice difference, refused while any metric is unresolved,
  because a minimum commitment applies to the invoice as a whole and a partial total cannot settle
  it.
- `UNEXPLAINED` → no impact, ever.

An assessor that cannot answer returns `impact=None` **with a reason**, never an estimate and never a
silent `None`. "No impact" and "not applicable" are different answers to a reviewer.

**Consequences.** Adding a hypothesis code requires a calculator, and `test_the_registry_covers_the_
closed_vocabulary` fails until one exists, so a code cannot ship silently unpriced. The trade-off is
that the registry is not extensible at runtime — there is no plugin mechanism — which is correct: an
impact calculator is arithmetic, and arithmetic belongs under test in one layer.

One genuine addition to Phase 2 was needed. §6.3's `UNAPPLIED_PAYMENT` reads `payments −
payment_allocations`, but Phase 2's `unapplied_payment_total` counts only money *allocated to another
invoice*. Money with no allocation at all — the more common case — was not visible to the
investigation. `unallocated_payment_total()` was added to `app/pricing/reconciliation.py` rather than
summed inside the assessor, so the arithmetic stays beside the other balance arithmetic and there is
one place for it to be wrong. The two conditions remain distinct in the output, because they need
different follow-up.

## ADR-024 — `COMPLETE` is refused when the interpretation is missing or caveated

**Status.** Accepted

**Context.** ADR-008 and FR-012 require that a degraded investigation is never presented as a clean
one. The natural way to satisfy that is discipline: return a status and trust every caller to check
it. That is exactly the assumption FR-012 exists to distrust, and it fails quietly — a status field
that is correct and a caller that does not read it.

The related risk is the inverse one. A provider can fail in a way that is indistinguishable from
success: returning a response with no findings means "the model failed" and "the invoice is clean"
produce the same object. §10 (F10) calls for a repair retry; this phase has no retry (ADR-026
below), so the failure has to be representable without one.

**Decision.** Two structural refusals in `InvestigationResult.__post_init__`, plus a fixed mapping from
failure to outcome:

- `status=COMPLETE` **requires** a validated interpretation and an empty degradation list.
- Any non-`COMPLETE` status **requires** a non-empty degradation list, so an unexplained degraded
  result cannot be constructed at all.

Failure outcomes, stated as data in `_FAILURE_OUTCOMES` and asserted by tests rather than inferred
from a chain of `except` clauses:

| Failure | Status | Why |
| --- | --- | --- |
| Not configured, timed out, provider error | `DEGRADED` | The model was unavailable; the deterministic findings stand alone |
| Response could not be parsed | `PARTIAL_FAILED` | A stage lost its output |
| Citation rejected | `PARTIAL_FAILED` | A stage lost its output |

The ordering of that table is load-bearing and is pinned by
`test_a_format_error_is_not_reported_as_an_unavailable_model`: `LlmResponseFormatError` subclasses
`LlmProviderError`, so a base-class-first mapping would report a lost stage as a missing model and
understate the failure.

**Consequences.** `investigate()` does not raise for a model-side failure; it returns the arithmetic
plus the failure, because the arithmetic is still worth having and the failure is part of the answer.
Programming errors and structural data errors still propagate — those are defects, not outcomes.

## ADR-025 — Confidence and likelihood cross the wire as strings, not JSON numbers

**Status.** Accepted

**Context.** §8.1 specifies `confidence` as a `Decimal` from 0 to 1. It is not money, so INV-01 does
not reach it. The same inexactness nevertheless applies: a provider emitting `"confidence": 0.82` in
JSON produces a Python `float`, and `Decimal(0.82)` recovers the nearest binary approximation to
`0.82` rather than `0.82`. ADR-001's reasoning — that conversion launders the error instead of
revealing it — is about the boundary, not about the amount.

**Decision.** `confidence` and `likelihood` are decimal *strings* on the wire and are parsed to
`Decimal` by `confidence_value()`. A JSON number is refused with a message naming the field, so a
provider can repair rather than guess. Bounds are enforced twice: by the parser for a clear message,
and by the field constraint for any other route into the model.

**Consequences.** A real adapter must instruct the model to emit strings, which the shipped
`system_instructions` do. The cost is a stricter schema than JSON requires, and it is a stricter schema
than this one needs in order to keep a probability exact.

## ADR-026 — No retries in the provider port

**Status.** Accepted

**Context.** ADR-013 specifies one repair retry for a schema failure, and `llm_max_retries` has been in
`app/config.py` since the foundation. A retry loop is easy to add and easy to get wrong: it needs a
justification against a specific failure mode, a bound, and a decision about what it costs.

**Decision.** No retry logic in `app/ports/llm.py`, and `llm_max_retries` is deliberately unread by it.
Failures are reported, not retried. The port docstring records what a retry would have to justify.

**Consequences.** A single transient network failure ends the interpretation stage as `DEGRADED`, which
understates a recoverable problem. That is the cheaper error: a retry policy added without evidence
about which failures are transient would multiply latency and provider cost while hiding the failures
it could not fix. Recorded here so the omission is visible as a decision rather than an oversight.

**Relationship to ADR-013.** ADR-013 (status: *Proposed — needs review*) specifies one repair retry for
a schema failure. Its Pydantic `extra="forbid"` validation is implemented; its retry is deliberately
not, per this ADR. ADR-013's open sub-question about linting narrative text for currency-shaped tokens
is also still unimplemented, and `test_narrative_prose_may_mention_an_amount_while_setting_no_
monetary_field` records that limitation rather than papering over it.

---

## Open questions

Tracked in full in `REQUIREMENTS_TRACEABILITY.md` §7. Condensed here with the decision each one blocks.

| OQ | Question | Blocks | Default assumed | Cost of being wrong |
| --- | --- | --- | --- | --- |
| OQ-01 | Identity source for analyst/reviewer roles | Auth, NEP-04 | Dev-mode headers, explicitly demo-grade | **High** — presenting headers as "auth" would be a false security claim |
| OQ-02 | Are adjustments pre-tax or post-tax? | FR-010, impact calculator | Pre-tax line subtotal; tax not recomputed | High — wrong tax handling is a real-money bug |
| OQ-03 | Rounding convention: `ROUND_HALF_UP` vs `ROUND_HALF_EVEN`? | Every computed amount | `HALF_UP` at invoice boundaries, `HALF_EVEN` for residual splits — now implemented in `pricing/rounding.py` and pinned by tests, so a different answer is a one-constant change | Medium — off-by-a-cent findings against the real billing system |
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
