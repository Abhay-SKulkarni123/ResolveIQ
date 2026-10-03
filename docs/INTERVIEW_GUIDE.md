# Interview Guide — ResolveIQ

A walkthrough for a reviewer. It is organised as a narrative, not a feature list, because the interesting
part of this project is *the reasoning*, not the endpoints.

**Read in this order:** `README.md` → this file → `docs/SYSTEM_DESIGN.md` → `docs/SOLID.md` →
`docs/ENGINEERING_DECISIONS.md` → `docs/REQUIREMENTS_TRACEABILITY.md`.

---

## 0. State of the repository — read this first

| Area | Status |
| --- | --- |
| Documentation (7 documents) | Drafted |
| `.env.example`, `.gitignore`, `docker-compose.yml`, `Makefile` | Present |
| `Money` value object + unit tests | **Implemented and verified** |
| FastAPI app factory + `/api/v1/health` | Implemented, import-verified |
| Database models, migrations, seed data | **Implemented and verified** (ADR-018…021) |
| Pricing engine (recalculation, rules, traces, reconciliation) | **Implemented and verified** (ADR-022) |
| Evidence pipeline, LLM adapters | **Not built** |
| Investigation / review / adjustment services | **Not built** |
| `app/services/` and `app/api/` beyond health | **Not built** |
| Frontend | **Not built** (directory only) |

Two things are genuinely finished and worth inspecting: the database foundation, and the deterministic
billing engine. Everything between the evidence bundle and the model — the investigation workflow, the
hypothesis registry, the review and adjustment services — is not built, and `app/api/` exposes only the
health endpoint. I would rather show two things that are correct than ten that are plausible.
`docs/REQUIREMENTS_TRACEABILITY.md` tracks this per requirement.

One honest caveat on verification: the database work was verified against a real PostgreSQL instance, and
the billing engine is verified by tests that need no database at all. The integration suite skips in this
checkout because no database is reachable, so treat "verified" above as scoped to what each suite actually
executed.

---

## 1. The 90-second version

A customer says: *"our invoice has 40,000 API calls we never made, and our February payment vanished."*

That is two different problems wearing one coat:

- **Is the arithmetic right?** Deterministic. Must be reproducible and auditable.
- **Which explanation fits the evidence?** Interpretive. Needs a model.

ResolveIQ splits them at a typed seam:

```
LLM  ──▶  hypothesis_code  ──▶  registry  ──▶  pure Decimal calculator  ──▶  Money + calculation_trace
      └─▶  findings, cited to evidence keys, with no money fields anywhere in the schema
```

The organising sentence — **the model chooses which rule might apply; deterministic code decides what the
rule means in money** — is the thing to evaluate. Everything else is consequence.

---

## 2. The three questions I would ask if I were reviewing this

### Q1 — "How do you know the model did not invent that number?"

Because the response schema **has no field that can hold a monetary amount** (`extra="forbid"`). Not "the
prompt says not to" — the parser rejects it.

This is `ADR-003`. The interesting follow-up is *why not* the weaker options: prompt-only instruction,
or accept-and-overwrite. Both leave hallucinated numbers inside the narrative where the human will read
them.

**Honest limitation I would volunteer:** a model can still write "roughly $400" in prose, and no schema
constrains prose. My mitigation is that the UI renders impact only from `computed_impact` +
`calculation_trace`, and narratives are labelled model-authored. A narrative lint for currency-shaped
tokens is a proposed mitigation I explicitly did **not** implement, because it false-positives on
legitimate references like "the $4,200 payment" (`ADR-013`, open sub-question).

### Q2 — "How do you prevent duplicate adjustments?"

Not with a disabled button. Five independent layers, in `SYSTEM_DESIGN.md` §9.3 and `ADR-007`:

1. Client `Idempotency-Key` + unique index — handles retries.
2. **Partial unique index** `ON adjustments(dispute_id) WHERE status IN ('PENDING','APPLIED')` — the layer
   that actually holds under concurrency, because the database is the last arbiter.
3. `SELECT … FOR UPDATE` on the dispute row — serialises two concurrent approvals so the second gets a
   clean `409` instead of a constraint violation.
4. Optimistic `version` column on `disputes` — protects other fields from lost updates.
5. State-machine guard on `PENDING_REVIEW → RESOLVED`.

The button is layer 0, documented as non-authoritative.

**The proof is a test, not a claim:** two threads, one barrier, both POST an adjustment, assert exactly
one `201` and one `409`. It is the first integration test in Phase 3. I would want that test to exist
before I claimed NEP-05 was met.

### Q3 — "What happens when the AI is wrong or unavailable?"

Three distinct outcomes, deliberately not collapsed into pass/fail:

| State | Meaning | Analyst sees |
| --- | --- | --- |
| `COMPLETE` | All stages succeeded | Full findings |
| `DEGRADED` | Succeeded on partial evidence (e.g. usage ingest lag) | Findings scoped to available evidence + explicit reason |
| `PARTIAL_FAILED` | A stage exhausted retries | Resumable from the first failed stage |

Per-stage persisted status is what makes this possible (`ADR-008`). A single boolean would make
"no evidence available" and "provider down" indistinguishable — different problems, different analyst
actions.

Related: **stale detection.** Every investigation stores an `evidence_fingerprint`; reviewer decisions
record the fingerprint they were looking at. Add evidence after approval and the approval is void,
enforced by a `409 INVESTIGATION_STALE`. Reviewers approve *a view of the evidence*, so if the evidence
moves, the approval should not survive (`ADR-006`, `ADR-007`).

---

## 3. Design decisions I would defend, with the trade-off stated

| Decision | Trade-off I accept |
| --- | --- |
| `Money` rejects `float` outright rather than coercing | Verbose call sites; a boundary bug becomes a loud error instead of a quiet wrong total |
| Hexagonal, not layered | One extra indirection layer, in exchange for a money engine that needs no DB or mocks |
| Closed hypothesis vocabulary | The model cannot invent a code; unknown ⇒ `UNEXPLAINED` with no impact |
| Citations validated by allowlist, whole response rejected on one bad key | A retry cycle costs a run; silently filtering would hide the exact failure we must surface |
| Hard failure over partial acceptance for LLM output | Fewer results, never a plausible-looking wrong one |
| Synchronous investigation, no queue | A slow provider holds a connection; bounded by timeout + resumable `PARTIAL_FAILED` |
| Append-only audit via `REVOKE`d grants | Needs separate migration/runtime roles |
| Evidence snapshots duplicated per dispute | Storage cost; buys reproducibility |
| No invoice/usage/payment tables yet; calculation inputs are value objects (ADR-022) | Nothing is wired to an API or repository; the whole engine is testable with no database, and the schema is not designed around a guess |
| Three line statuses rather than two, so "cannot be verified" is not "no discrepancy" | More branches in every consumer of a result; a partially verified invoice can no longer be presented as clean |

I would also point at what I *rejected* — `SOLID.md` has an explicit "rejected abstractions" table
(generic `Repository[T]`, event bus, `BillingProvider` for a billing system that does not exist, DI
container, generic `Rule` base) and a "where I deliberately did NOT apply SOLID" section. Over-applying
SOLID is a failure mode, and claiming restraint is part of the answer.

---

## 4. Walkthrough of the money path

This is the part I would want to inspect line by line, because it is where correctness is hardest.

```
usage_events (41,234 calls in period)
   └─ contract_price_terms: 10,000 included, tiers [10k–50k @ 0.00070], [50k+ @ 0.00050]
        └─ TieredOverageCalculator  (pure; stdlib decimal only)
             └─ line amount 21.86  (ROUND_HALF_UP)
                  └─ calculation_trace: 4 ordered steps, each with its expression and inputs
                       └─ written as NUMERIC(19,4); quantised to 2dp at presentation only
```

Points to check:

- Does any step use `float`? (`Money` raises `TypeError`; there is also a test that walks the AST.)
- Is the rounding mode a named constant rather than an inline literal? (`pricing/rounding.py`)
- Is there a `calculation_trace` attached, so an analyst can find the step that produced a number they
  disagree with?
- Is `ROUND_HALF_EVEN` used for residual distribution on purpose? Yes — it minimises systematic bias when
  splitting a total across many rows. It is intentional and tested, and `OQ-03` asks whether the client's
  billing system agrees.

---

## 4a. The billing engine, term by term

Five concepts carry the whole design. Each is given as a definition, a worked example, and why it is
that way, because a rule with no example is a rule nobody can check.

### 4a.1 Exact decimals — never floats

**Definition.** Every monetary and metered value is a `Decimal`, wrapped in `Money`. A binary float is
rejected at the boundary rather than converted.

**Example.** 0.1 + 0.2 is 0.30000000000000004 in binary floating point. A rate of $0.00070 per call over
41,234 calls is $28.8638 — and `0.0007 * 41234` in float arithmetic does not reliably give you that. The
engine computes `Decimal("0.00070") * 41234 = Decimal("28.86380")`, exactly.

**Why.** Converting a float to `Decimal` does not recover the value the author intended; it recovers the
nearest binary approximation, so the conversion launders the error instead of revealing it. Refusing
floats at construction means the mistake surfaces at the point it is made, naming the field. The rule is
enforced mechanically by `tests/unit/test_no_float_in_calculations.py`, which walks the AST of `domain/`
and `pricing/` and rejects float literals, `float()` calls, `round()`, and int/int division.

### 4a.2 One rounding, at the line boundary

**Definition.** Intermediate amounts are exact. A line amount is rounded exactly once, on the way out.

**Example.** A ladder with two tiers at $0.005 each, over 6 units: 3 × 0.005 = $0.015 and 3 × 0.005 =
$0.015, summing to $0.030, which rounds to $0.03. Rounding each tier on the way past would give $0.02 +
$0.02 = $0.04.

**Why.** If the total depended on how many tiers the contract happened to have, the same usage would cost
more under a longer ladder. That is not a rounding preference, it is a correctness requirement — and the
two-tier example above is a case where the two answers differ by a cent.

### 4a.3 A rate is never invented

**Definition.** Where the contract does not state a rate, the engine reports that it cannot price the
line rather than extrapolating.

**Example.** Usage runs to 1,500 billable units but the ladder stops at 1,000. The line comes back
`UNRESOLVED` with reason `USAGE_EXCEEDS_TIER_LADDER`, carrying the 1,000 units the ladder did cover and
the quantity found. No amount is attached.

**Why.** Extrapolating the last tier's rate, or charging the excess at zero, would both produce a
confident number that no contract agreed to. In a dispute that number is worse than no number, because it
looks like an answer.

### 4a.4 A discrepancy is a result, not a diagnosis

**Definition.** The engine reports what the contract supports beside what the invoice charged, and stops
there.

**Example.** Recalculated $21.86 against recorded $28.40, difference −$6.54. That is the entire output. It
does not claim the cause. Whether that is a duplicated usage event, a wrong tier boundary or a stale
contract version is the investigation's job.

**Why.** An amount that arrives with a cause attached is harder to challenge: a reviewer must disprove the
diagnosis before they can trust the arithmetic. Keeping them apart lets the arithmetic stand on its own
while the diagnosis is argued separately. It is also why `ACCEPTED_AS_RECORDED` and `UNRESOLVED` are
distinct statuses — a flat fee that cannot be verified is not the same finding as a usage line with no
contract term, and collapsing them would let a partially verified invoice read as a clean one.

### 4a.5 The balance is a separate question from the arithmetic

**Definition.** `reconcile_balance()` lives in its own module and computes
`recalculated total − allocated payments − net adjustments`. It uses the recalculated total, never the
invoice's stated total.

**Example.** A $120.00 invoice recalculates to $120.00, a $50.00 payment is allocated to it, and a $10.00
credit is issued. Outstanding: $60.00. The same $50.00 payment with *no recorded allocation* leaves the
full $120.00 outstanding — the money exists, but nobody has said which invoice it pays, and §6.3 lists
`UNAPPLIED_PAYMENT` as a cause of dispute precisely for that state.

**Why.** A balance depends on events that happened *after* the invoice — a late payment, a goodwill credit,
a write-off — so merging it with the recalculation would make the arithmetic impossible to verify without
also asserting something about account state. Using the stated total would be worse: the two would agree by
construction and the exercise would be pointless. Two refusals matter most here. An overpayment is
reported as a negative balance rather than clamped to zero, because the negative figure *is* the finding
that a refund is due. And a balance built on a partial recalculation is marked provisional, so a small
number cannot read as a verified zero.

---

## 5. Security: what is real and what is a labelled stub

I would rather be blunt about this than let a reviewer assume more than exists.

| Control | Status |
| --- | --- |
| No credentials in frontend; LLM key backend-only; `/capabilities` returns no key material | **Real** |
| `.env` gitignored, `.env.example` placeholders only | **Real** |
| Untrusted dispute text treated as data, capped at 10,000 chars | **Real** (schema + allowlist + prompt framing) |
| SQL injection | **Real** — SQLAlchemy parameter binding only, no string-built SQL |
| PCI | **Real** — payments store `method` + `last4`, never card data |
| Audit immutability | **Real** — `REVOKE UPDATE, DELETE` from the app role |
| **Analyst/reviewer authentication** | **NOT REAL.** Dev-mode headers (`X-Actor-Id`, `X-Actor-Role`). Trivially spoofable. |

That last row is the honest answer to "is auth implemented?" — **no**, and it is the highest-risk
assumption in the project (`OQ-01`). Role separation between analyst and reviewer is a *design* decision
that is real; *enforcement* is a stub. Presenting a header check as "authentication" would be a false
security claim, which is why it appears in three places: `SYSTEM_DESIGN.md` §11.2, `ADR-015`, and the
open-questions table.

---

## 6. Prompt injection — the honest answer

The customer's dispute description is attacker-controlled text that ends up in a prompt. My defences:

1. The description is in a clearly delimited data section with an explicit instruction that it is content
   to analyse, not instructions to follow.
2. Output is schema-constrained — the model cannot introduce new fields, actions, or endpoints.
3. Citations are allowlisted against the case bundle, so injected text cannot reference evidence that
   does not exist.
4. The model has **no capability to act**: it cannot approve, cannot create an adjustment, cannot call a
   tool. Its output is a proposal that a human must accept (`NEP-04`).

Point 4 is the structural defence and the reason I care less about clever prompt hardening. Capability
restriction beats prompt defence.

**Residual risk, stated plainly:** not eliminated. A model can still be misled into a *wrong but
well-formed and correctly-cited* conclusion. That is why human approval is mandatory and why findings
show confidence and citations rather than verdicts.

---

## 7. Testing strategy

| Layer | Needs DB | Needs network | Proves |
| --- | --- | --- | --- |
| `unit/` | No | No | `Money`, pricing rules, state machines, citation validator, fingerprints, ORM schema declarations |
| `contract/` | No | No | Every `LlmProvider` obeys the same safety obligations |
| `integration/` | Yes | No | Repos, migrations, **every database constraint**, and the concurrency test for NEP-05 |
| frontend unit | No | No | Components, money *formatting*, staleness badges |
| frontend e2e | Yes | No | Dispute → investigate → review → adjust |

Nothing under `app/domain/` or `app/pricing/` may import `sqlalchemy`, `fastapi`, or another `app/` layer.
`tests/unit/test_layer_boundaries.py` walks their ASTs and fails on a forbidden import, which is what
makes the dependency inversion in ADR-002 real rather than aspirational. The check runs against `app/`
rather than against `tests/`, because a test for the ORM adapter has to import the ORM adapter; see
`docs/SOLID.md` for the clarification this required.

The **golden-fixture** approach is worth mentioning: the seed data has a known-bad invoice with a
hand-computed correct answer, so the pricing engine is tested against a human-derived expected value, not
against whatever the code currently produces.

---

## 7a. Talking about the database layer

This is the part most candidates wave through, so it is worth being able to defend in detail.

**"How do you know the money is exact?"** Three independent layers, and I would name all three:

1. `Money` (ADR-001) refuses a `float` at construction rather than coercing it, because
   `Decimal(0.1)` is not `0.1` and coercing hides a bug instead of fixing it.
2. The columns are `NUMERIC(19,4)` (ADR-011). A test asserts that no column in the schema has a
   floating-point type, so the guarantee survives someone adding a column later.
3. The domain never mutates the ambient `decimal` context, so precision does not depend on which
   thread or request happened to run first.

**"Why 19,4 and not 10,2?"** Because a unit price of `0.0007` per API call has to be storable. With two
decimal places it rounds to `0.00` or `0.01` and the error is invisible until a customer disputes an
invoice. There is an integration test that writes `0.0007` and reads back `0.0007`.

**"What does the database actually enforce?"** Only what is true of the *representation*: precision,
non-negativity, currency format, and that a `tier_schedule` exists exactly when the mode is `TIERED`.
What is true of the *business* — that a commitment term has a floor, that tier thresholds ascend — stays
in the domain. Putting business rules in `CHECK` constraints means writing the same rule twice, in two
languages, and watching them disagree. That line is ADR-020, and the one place I knowingly accepted a
weaker database is the internal shape of the JSON tier ladder, which the pricing layer validates instead.

**"What happens on delete?"** `RESTRICT` on every foreign key, written out explicitly even where it is
PostgreSQL's default. In a billing system a cascade deletes the financial history a dispute is about, as
a side effect of removing a parent row. There are two integration tests that try to delete an account with
contracts and a contract with price terms, and assert PostgreSQL refuses.

**"How do you know the migration matches the models?"** Three ways: `alembic check` in CI, an
`upgrade → downgrade → upgrade` round trip that compares the constraint sets after each pass, and a unit
test that renders both the migration's DDL and the models' DDL and compares them column by column. The
last one is the one that has caught real bugs — it is how I found that a `CHECK` constraint written with
its already-prefixed name was being double-prefixed by the naming convention, and how the longest names
were arriving from PostgreSQL truncated to 63 characters with a hash suffix.

**"How do you know your tests are not lying?"** The destructive part runs against a separate disposable
database, and a fixture refuses to run if the database name is not the expected one. The role that runs
the tests is not a superuser, so a test run cannot quietly escalate its own privileges (ADR-021).

---

## 8. Assumptions and unresolved questions

Full list with blocking impact in `docs/ENGINEERING_DECISIONS.md` §"Open questions". The five that matter:

| OQ | Question | Why it matters |
| --- | --- | --- |
| **OQ-01** | Where does identity come from? | Highest risk. Currently a header stub. |
| **OQ-02** | Are adjustments pre-tax or post-tax? | Affects every computed impact. Wrong answer = real-money bug. |
| **OQ-03** | `ROUND_HALF_UP` or `ROUND_HALF_EVEN`? | Off-by-a-cent findings against the real billing system. |
| **OQ-06** | Retention/PII policy for dispute text and snapshots? | Compliance. |
| **OQ-09** | Volume and latency targets? | Decides whether ADR-016 (sync, no queue) survives. |

I have defaulted each one, marked it as an assumption in the traceability matrix, and kept it visible
rather than inventing a requirement.

---

## 9. If I had more time

In priority order, and stated as sequencing rather than wish-list:

1. **Finish the concurrency proof** (NEP-05 integration test). It is the requirement most likely to be
   challenged and the cheapest to settle definitively.
2. **Close OQ-02/OQ-03 with the client.** These change computed numbers, so they block the pricing
   engine's fixtures, not just its code.
3. **Replace header auth with real token verification** (OQ-01), so role separation is enforced rather
   than modelled.
4. **Add a second `LlmProvider`** against the contract suite. This is what proves ADR-009's substitutability
   claim rather than asserting it.
5. **Golden-fixture expansion** across every `hypothesis_code`.
6. **Hash-chained audit events** if compliance requires tamper-evidence (ADR-010 names this as not solved).
7. **Frontend e2e** for the full analyst journey.

---

## 10. Questions I would ask about the brief

Genuine ambiguities I hit while designing, in case they are also ambiguities for the reviewer:

1. "Invoice **may not** reflect a previous payment" — is that a *suspicion* to investigate, or a known
   bug class to reproduce? It changes whether the payment-reconciliation path is primary or secondary.
2. Should the system be able to conclude **"the invoice is correct"**? I assumed yes (`NO_ADJUSTMENT`), on
   the grounds that a dispute tool that always finds a cause is not trustworthy — but it is possible the
   assessment expects a cause to be found.
3. Is a single reviewer enough, or is separation of duties (different people for propose/approve) in
   scope? I implemented one reviewer and recorded the question.
4. Is the adjustment expected to be visible in an external system (a real ledger call), or purely a record
   in this system? The brief says "mock adjustment", which I read as the latter.
5. Should evidence come from an ingest step, a live connection, or uploaded files? I chose ingest + snapshot
   (`ADR-006`) because it makes findings reproducible.
