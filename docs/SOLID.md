# SOLID in ResolveIQ — concrete, not decorative

SOLID is a set of heuristics for managing change. It is worth nothing here unless each principle is tied
to a **specific class in this repository** and a **specific failure it prevents**. This document does
that, and — just as importantly — records where I deliberately did *not* apply a principle.

Read `docs/SYSTEM_DESIGN.md` first for the domain vocabulary.

---

## Contents

- [How the layers map to SOLID](#how-the-layers-map-to-solid)
- [S: Single Responsibility](#s-single-responsibility)
- [O: Open/Closed](#o-openclosed)
- [L: Liskov Substitution](#l-liskov-substitution)
- [I: Interface Segregation](#i-interface-segregation)
- [D: Dependency Inversion](#d-dependency-inversion)
- [Abstraction ledger](#abstraction-ledger-the-nep-10-discipline)
- [Where I deliberately did NOT apply SOLID](#where-i-deliberately-did-not-apply-solid)
- [Common misapplications to avoid in review](#common-misapplications-to-avoid-in-review)

---

## How the layers map to SOLID

SOLID is about *classes and modules*, not about folders. In ResolveIQ it shows up at two levels:

| Level | What it means here |
| --- | --- |
| **Module level** | `domain/`, `pricing/`, `services/`, `api/`, `adapters/` each have one reason to change. This is closer to DIP + SRP than to classic SOLID. |
| **Class level** | Inside `pricing/` and `services/`, individual classes each own one rule or one use case. This is where OCP and LSP do their real work. |

Both matter. The layer boundaries are enforced mechanically (import checks, `SYSTEM_DESIGN.md` §4); the
class boundaries are enforced by review and tests.

---

## S: Single Responsibility

> A module should have one reason to change.

"Reason to change" is the operative phrase. Not "does one thing" — that is a slogan. If I can name the
single sentence in the requirements that would force me to edit this file, SRP is satisfied.

| Class / module | Single responsibility | The one sentence that would change it |
| --- | --- | --- |
| `domain/money.py` (`Money`) | Represent and safely combine an exact amount + currency. No persistence, no formatting policy, no FX. | "Money must also convert between currencies." (currently rejected — OQ-04) |
| `pricing/engine.py` | Given a `hypothesis_code`, find and invoke the right calculator. Dispatch only. | "The engine must also choose the hypothesis." (rejected — that is the LLM's job, §7 of design) |
| `pricing/tiered.py` | Evaluate one tiered/committed pricing term against a usage summary. | "Tiering must also consider discounts." (new term type, not this file's concern) |
| `pricing/rounding.py` | The single place a rounding mode is chosen, per boundary. | "Rounding policy differs per customer." (→ configuration, still one file) |
| `services/evidence.py` | Assemble, hash, and freeze the immutable evidence bundle; compute fingerprints. | "Evidence should be fetched live from billing." (rejected — breaks reproducibility) |
| `services/investigation.py` | Orchestrate the stage machine; decide what runs next; persist per-stage outcomes. | "Investigation should auto-approve low-risk cases." (rejected — violates NEP-04) |
| `services/adjustment.py` | The adjustment state machine and its five duplicate-prevention guards. | "Adjustments should support partial application across invoices." (new use case, new service method — not a second responsibility) |
| `services/audit.py` | Append audit events. Never update, never delete, never interpret. | "Audit events need redaction." (→ append a redaction event; history stays immutable) |
| `adapters/llm/mock.py` | Return a deterministic interpretation for a given request fixture. | "The mock should be smarter." (→ it should not be. It is a test double.) |

**Where SRP is most often violated in this kind of project:** putting `evidence collection`, `prompt
building`, `LLM calling`, and `response parsing` in one `investigation_service.py` of 800 lines. That is
one class with four reasons to change, and it cannot be unit-tested without mocking the network. The
split here is: `EvidenceService` (collect/hash) → `InterpretationService` (prompt/parse/validate) →
`ImpactCalculator` (pure) → `InvestigationService` (orchestrate only).

---

## O: Open/Closed

> Open for extension, closed for modification.

The test: adding a new pricing rule or a new LLM provider must not require editing existing, working code.

### Extension point 1 — pricing rules (strategy pattern)

```python
# pricing/registry.py
CALCULATORS: dict[str, ImpactCalculator] = {
    "OVERAGE_TIER_MISMATCH": TieredOverageCalculator(),
    "UNIT_PRICE_MISMATCH": UnitPriceCalculator(),
    "DUPLICATED_USAGE": DuplicateUsageCalculator(),
    # ...
}
```

Adding a rule = one new class + one registry line + tests. No existing calculator is touched. The
alternative — an `if/elif` ladder in `engine.py` — would be OCP's exact violation and would grow a new
branch for every hypothesis the LLM learns to emit.

The `hypothesis_code` vocabulary is closed on purpose: the LLM cannot invent a code and thereby invent a
calculation. Unknown code ⇒ `UNEXPLAINED`, never a guess.

### Extension point 2 — LLM providers (`LlmProvider` protocol)

`InterpretationService` depends on the protocol. `MockLlmProvider` and a future real adapter are
substitutable without a single edit in `services/` or `domain/`.

### Extension point 3 — resolution options

`ResolutionPlanner` maps `hypothesis_code` → candidate `option_type`s via a table. New remedy, new table
entry.

### Where OCP is *not* applied

`domain/money.py` is **closed** for a good reason: allowing arbitrary new "money-like" behaviour is how
float creeps back in. Not every module should be extensible. Applying OCP reflexively is its own smell.

---

## L: Liskov Substitution

> Anything that can be used in place of a type must actually behave like that type.

LSP matters most for `LlmProvider`, because a broken provider would corrupt findings silently.

Every implementation must satisfy, and `tests/contract/test_llm_provider_conformance.py` asserts:

| Contract | Why it is part of LSP, not "extra" |
| --- | --- |
| Returns a schema-valid `InterpretationResponse` or raises | A provider returning `None` or a dict "sometimes" is not substitutable |
| **Never** returns monetary amounts | Substituting a sloppy provider must not be able to inject invented numbers into a system whose whole premise is that money is computed deterministically |
| Cites only keys from the supplied bundle | Substituting a hallucinating provider must not be able to invent evidence |
| Honours the timeout contract | A provider that hangs must not substitute cleanly |
| `MockLlmProvider` is deterministic for identical input | Required for the suite to mean anything |

The third row is the important design decision: **LSP is enforced as an obligation on implementers, not
trust in callers.** The system degrades safely when a provider misbehaves, because substitutability
includes the safety properties.

A note on `Money`: it is `frozen=True` and validates in `__post_init__`, so a `Money` instance cannot be
in an invalid state. LSP for a value object means "there is no invalid instance to substitute" — the type
makes the contract unrepresentable rather than checked.

---

## I: Interface Segregation

> Clients should not be forced to depend on methods they do not use.

The concrete offender in codebases like this is one wide `Repository` interface with 40 methods and a
`FakeRepository` that implements all of them, most as `raise NotImplementedError`.

ResolveIQ splits by **role**, not by table:

| Protocol | Used by | Deliberately excludes |
| --- | --- | --- |
| `EvidenceReader` | `EvidenceService` | Anything that writes |
| `DisputeWriter` | `InvestigationService` | Bulk reads, deletes |
| `AdjustmentRepository` | `AdjustmentService` | Evidence, findings |
| `AuditSink` | everything | Reads — the log is written, never queried through the port |
| `UnitOfWork` | services needing transactional scope | Business rules |

Consequence: the test double for `EvidenceReader` is six methods, not forty. The cost of a wide interface
is paid at test time, which is where it hurts.

**Honest trade-off:** these protocols are close to one-per-table, which can look like pointless ceremony.
The justification is narrow and concrete: `AuditSink` has *one* method (`append`). Making it depend on a
40-method `Repository` would mean every test asserting "an audit event was written" also had to satisfy
39 unrelated methods. That is the ISP violation made mechanical. If a protocol grows beyond ~8 methods
without a clear reason, it should be split — that is the review trigger.

---

## D: Dependency Inversion

> High-level policy should not depend on low-level details. Both should depend on abstractions.

This is the principle that makes the money engine and state machines testable with **no database and no
network**.

```
services/  ──depends on──▶  ports/  (Protocols: LlmProvider, EvidenceReader, UnitOfWork)
                                   ▲
                                   │ implemented by
adapters/  ──implements──────────┘  (SQLAlchemy repos, MockLlmProvider, real HTTP client)
```

Concretely:

| High-level policy | Depends on | Never depends on |
| --- | --- | --- |
| `InvestigationService` | `LlmProvider`, `EvidenceReader`, `UnitOfWork` | `openai`, `httpx`, SQLAlchemy `Session` |
| `AdjustmentService` | `AdjustmentRepository`, `AuditSink`, `UnitOfWork` | FastAPI `Request`, raw SQL |
| `pricing/*` | nothing but `domain.money` and stdlib `decimal` | literally everything else |
| `api/` routers | service classes via `Depends` | repositories, SQL |

Wiring happens in exactly one place — `app/api/deps.py` (composition root) — which is the only module
allowed to know both FastAPI and SQLAlchemy. This is why `pricing/` unit tests need no fixtures, no
container, and no `pytest.mark.asyncio`: they are ordinary function tests.

**The test that proves DIP is real:** nothing under `app/domain/` or `app/pricing/` may import
`sqlalchemy`, `fastapi`, `pydantic`, or any other `app/` layer. If one does, an abstraction has leaked.
This is checked by `tests/unit/test_layer_boundaries.py`, which walks the AST of every module in those two
packages and fails on a forbidden import.

**One clarification to what this rule said before it was implemented.** The original wording was "`tests/unit/`
must import nothing from `adapters/`". Enforced against the whole test tree, that rule bans the ORM tests:
a test for the persistence adapter necessarily imports the persistence adapter, so the only way to satisfy
it would be to not write those tests. The invariant actually worth protecting is that *production* inner
code stays free of outer dependencies, so that is what the check asserts, against `app/` rather than against
`tests/`. The ORM tests live in `tests/unit/test_persistence_models.py` and
`tests/integration/test_constraints.py`, and they are free to import the adapter they are testing.

Two guards keep the check from passing vacuously: one asserts that each layer actually contains modules,
because an empty glob would satisfy every assertion, and the other names the specific rule in the failure
message rather than listing forbidden module names.

---

## Abstraction ledger: the NEP-10 discipline

NEP-10 says keep it simple and justify every abstraction. Each interface in `app/ports/` must be able to
answer: *which concrete problem does this solve?*

| Abstraction | Solves | Would you delete it if the problem went away? |
| --- | --- | --- |
| `LlmProvider` | Swappable AI; tests need determinism and no network | Yes — but STK-06 mandates it |
| `Money` | Makes float unrepresentable in the money path | **No** — it is the enforcement mechanism for NEP-01, not an indirection |
| `EvidenceReader` / `DisputeWriter` / `AdjustmentRepository` | Test doubles without a database | Yes, if we accepted integration-test-only coverage |
| `AuditSink` | ISP: 1-method dependency; guarantees append-only usage | Yes |
| `UnitOfWork` | Transaction boundary for the concurrency guards (NEP-05) | No — `SELECT … FOR UPDATE` needs a real transaction scope |
| `ImpactCalculator` per rule | Independent testing of each pricing rule; OCP | Yes, in principle — but see below |
| `InterpretationRequest/Response` | Pydantic validation of untrusted model output (NEP-08) | No |

**Rejected abstractions** (recorded so they are not re-proposed):

| Rejected | Why |
| --- | --- |
| Generic `Repository[T]` base class | Saves ~20 lines, couples the domain to SQLAlchemy's query API, and makes every repository look alike. Worse, it invites business logic into repositories. |
| Event bus / CQRS framework | No second consumer exists. Speculative infrastructure for an assessment. |
| Abstract `BillingProvider` for "multiple billing systems" | There is exactly one, and it is ingested into our tables. Premature. |
| DI container (e.g. `dependency-injector`) | FastAPI `Depends` already is one. |
| Plugin registry with entry points | No third-party plugins. |
| Generic `Rule` base class for pricing | Each pricing rule has genuinely different inputs; a lowest-common-denominator base would be a worse abstraction than no base. |

---

## Where I deliberately did NOT apply SOLID

Over-application is a real failure mode, and an interviewer will probe for it.

1. **No interface for `TieredOverageCalculator`.** It is a pure function with no alternative
   implementation. Adding `ITieredOverageCalculator` would be ISP/LSP theatre — a one-implementation
   interface with no substitutable behaviour is pure indirection. The registry (`CALCULATORS`) provides
   the polymorphism where it is actually needed.
2. **Services are concrete classes, not interfaces.** `InvestigationService` is used directly. It has one
   implementation and no alternative. An `IInvestigationService` would be DIP misapplied — DIP is about
   depending on *volatile* details (I/O, providers), not about interface-everything.
3. **No micro-methods.** SRP does not mean a class per method. `AdjustmentService.create()` is
   ~80 lines but has exactly one reason to change. Splitting it into
   `ValidateApprovalCommand` / `GuardDuplicateCommand` / `PersistAdjustmentCommand` would scatter one
   transaction across classes and make atomicity *harder* to reason about. This is the SRP/transaction
   tension, and cohesion of a single transaction beats method-count purity.
4. **`Money` does not implement a factory/builder.** One constructor with strong validation is simpler
   than a builder and cannot produce an invalid instance.

---

## Common misapplications to avoid in review

| Misapplication | What it looks like | Correct response |
| --- | --- | --- |
| "SRP means small classes" | A 15-line class per field access | Ask for the *reason to change*, not the line count |
| "DIP means wrap everything" | `IStringFormatter` with one implementation | Wrap only volatile boundaries: I/O, providers, clocks |
| "OCP means no `if` statements" | A plugin system for three fixed rule types | Table-driven dispatch is enough |
| "LSP means same method names" | Renaming methods to satisfy a protocol | LSP is about *behavioural* substitutability, verified by the conformance suite |
| "ISP means one interface per class" | 12 one-method protocols for one class | Split by *client role*; if there is one client, there is one interface |
| Layering as SOLID | "We have 5 folders so we're SOLID" | Folders are not principles; the dependency rule is testable, folder names are not |
