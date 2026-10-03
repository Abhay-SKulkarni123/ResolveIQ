# Agent Usage — ResolveIQ

An honest record of how this repository was produced, so a reviewer can judge the process and not just
the output.

**Scope of this entry:** Phase 0 (foundation) only, completed in a single session on 2026-10-03.

**A note on what "agent" means here.** The implementation partner for this work was an AI coding agent
(`opencode`, model `big-pickle`) operating in this session, plus the human reviewer who set the task. This
document records what that agent actually did. Nothing is reconstructed from memory or invented to make
the record look better than it was.

---

## 1. Environment inspection (performed before any design work)

The agent inspected the working directory and available tooling **first**, as instructed, and the findings
directly shaped later decisions.

| Probe | Command | Result |
| --- | --- | --- |
| Working directory contents | `ls -la` | **Completely empty** — greenfield |
| Python | `python --version` | `Python 3.10.9` |
| Python alias | `python3 --version` | **Not found** (Windows) |
| Node | `node --version` | `v24.13.0` |
| npm | `npm --version` | `11.14.0` |
| Docker CLI | `docker --version` | `Docker version 29.5.3` |
| Compose | `docker compose version` | `Docker Compose v5.1.4` |
| psql client | `psql --version` | `psql (PostgreSQL) 18.4` |
| git | `git --version` | `2.51.0.windows.1` |
| `uv` | `uv --version` | **Not installed** |
| `poetry` | `poetry --version` | **Not installed** |
| `make` | `make --version` | **Not installed** |
| Network reachability | urllib HEAD to `pypi.org/simple/` | HTTP `200` — package installation possible |

**Decisions these findings forced:**

- `requires-python = ">=3.10"` and `target-version = "py310"` rather than a newer baseline, because the
  only interpreter available is 3.10.9. Everything written is 3.10-compatible.
- The Dockerfile uses `python:3.12-slim`. **This is a deliberate mismatch** with the local 3.10
  interpreter, chosen because the container is the deployment target and 3.12 is a sound production
  baseline. Documented rather than silently assumed compatible.
- No `uv`/`poetry`: plain `venv` + `pip install -e ".[dev]"` is the documented path.
- **No `make`:** the `Makefile` was still written (it is convenient where available), but every target has
  the raw command documented in `README.md` §"Verify the setup" so the build is not gated on a tool that
  is absent. This is called out in both the README and the `Makefile` header.
- `psql` 18.4 client is present but no server; the database is expected to come from Compose.
- The directory being empty meant scaffolding was safe, satisfying the brief's conditional.

---

## 2. Tools used

| Tool | Used for | Notes |
| --- | --- | --- |
| `bash` | Env inspection, venv creation, dependency install, running pytest/ruff/mypy, booting uvicorn, `docker compose config`, HTTP checks via `curl` | Primary working tool |
| `write` | Creating all new files (docs, code, config) | 20 files |
| `edit` | Targeted corrections after review (see §5) | 9 edits |
| `read` | Verifying file contents before/after edits | 2 reads |
| `grep` | Verifying traceability table rows after a bulk edit | 2 calls |
| `glob` | Directory scaffolding | 1 call |
| `todowrite` | Task tracking across the phase | 11 items |
| `websearch` / `webfetch` | — | **Not used.** No external research was needed; the design came from the brief and the domain. |
| `task` (subagent delegation) | — | **Not used. See §4.** |

---

## 3. Representative prompts

The human's instructions, abridged to the operative lines. These are quoted from the session, not
paraphrased from a plan.

**Opening / framing**
> "We are building ResolveIQ, an AI-assisted Billing Dispute Investigation and Resolution Agent for a
> hiring assessment. Act as a senior software architect and implementation partner. Work incrementally,
> inspect your environment first, and explain important decisions. **Do not generate the entire
> application in one pass.**"

**First task (Phase 0 scope)**
> "1. Inspect the current directory and available development tools.
> 2. Propose a maintainable repository structure...
> 6. Propose an implementation sequence with acceptance criteria for each phase.
> 7. List assumptions and unresolved decisions rather than silently inventing requirements.
> 8. If the directory is empty, prepare the initial scaffold only if doing so is safe and consistent with
> the plan.
> Do not implement the complete business logic yet. **Do not claim anything has been tested or deployed
> unless you actually verified it.**"

**Documentation requirement that shaped the whole session**
> "`AGENT_USAGE.md` must accurately document the tools used, representative prompts, delegated work,
> important agent mistakes or rejected suggestions, and verification performed. **Do not invent agent
> activity.**"

**Closing instruction**
> "We will review the foundation before moving to implementation."

The instruction not to over-claim was load-bearing: it is why §6 records a failed Docker startup and why
`README.md` carries a status table rather than a feature list.

---

## 4. Delegated work

**No subagents were spawned. Zero `task` tool invocations.**

All exploration, design, writing, and verification was done directly by the agent. The repository was
small enough (greenfield, ~20 files) that delegation would have cost more in context transfer than it
saved, and the brief asked for incremental work with explained decisions — which favours a single
continuous thread of reasoning that the agent could keep consistent across seven interdependent documents.

*(`docs/INTERVIEW_GUIDE.md` recommends the Task tool for open-ended codebase searches. That guidance was
written for later phases, when a real codebase exists to search. It was not applicable here.)*

---

## 5. Mistakes made, and rejected suggestions

Recorded because a clean-looking log would be less useful than an accurate one. **Every item below was
actually made and actually caught.**

### 5.1 A real code bug — caught by the agent's own test

`Money.allocate()` initially split an amount across weights at **whole currency units** instead of at the
currency's **minor-unit** resolution. Splitting `10.00 USD` three ways produced `3 / 3 / 4` instead of
`3.33 / 3.33 / 3.34` — it destroyed 98 cents.

It was caught immediately by the parameterised conservation test, which failed on two cases:

```
FAILED test_allocation_conserves_money[0.05-weights2]      assert Decimal('0') == Decimal('0.05')
FAILED test_allocation_conserves_money[7.77-weights5]      assert Decimal('7') == Decimal('7.77')
FAILED test_allocation_gives_remainder_to_largest_fractional_parts
3 failed, 52 passed
```

**Fix:** rewritten to work in integer minor units (`int(quantised_amount.scaleb(places))`), doing the
division and residual distribution in exact integer arithmetic, which is also immune to the ambient
decimal context. All 59 tests then passed.

*Why it matters:* this is precisely the class of bug NEP-01 exists to prevent, and it survived into the
first draft of the one file whose whole job is to make that impossible. The test did its job.

### 5.2 Documentation defects introduced and then fixed

| Defect | How found | Fix |
| --- | --- | --- |
| `SYSTEM_DESIGN.md` table of contents numbered sections 1–16 in an order that did not match the actual body sections (money was §4 in the TOC and §6 in the body), so every anchor link was broken | Read-through after writing | Rewrote the TOC to match the body; verified all 17 anchors |
| Cross-references went stale after that renumbering — traceability rows still pointed at `§11.4`, `§10.3`, `§5.6`, `§11.3`, sections that no longer existed | Systematic grep of the traceability table | Bulk-corrected ~20 references; individually re-checked `NEP-04`, `NEP-09`, `NEP-10`, which the bulk pass had missed |
| ADR-013's table-of-contents title was garbled: `` `stripe`-style structured output validation `` — text from an unrelated draft | Re-reading the ADR index | Corrected to "Pydantic-validated structured output with one repair retry, then degrade" |

### 5.3 Tooling violations found by the tools, not by inspection

Running the linters I had configured — rather than assuming they were clean — surfaced eight issues:

- `RUF100` unused `# noqa: S104` (bandit rules not enabled)
- `RUF022` unsorted `__all__` in `money.py`
- `B015` ×2 "pointless comparison" — comparisons inside `pytest.raises` read as dead code; fixed by
  assigning (`_ = usd < eur`) so the assertion is on the *operation raising*
- `RUF005` list concatenation instead of unpacking
- `ruff format` reformatting 2 files (stray trailing whitespace in a package `__init__.py`)
- `mypy --strict` ×4 "unused `type: ignore`" — mypy special-cases `return NotImplemented`, so the
  suppressions were unnecessary noise; removed

**Lesson worth stating:** the `# type: ignore[return-value]` comments were added defensively without
checking whether mypy needed them. Configuring a type checker and then not running it would have shipped
four pieces of unjustified noise.

### 5.4 Configuration that referenced things that did not exist

Found by attempting to run the stack rather than by re-reading the YAML:

- `docker-compose.yml` ran `alembic upgrade head` on API startup, but no migration existed — the service
  would have crash-looped on a cold `docker compose up`.
- `backend/Dockerfile` did `COPY alembic.ini ./alembic.ini`, but no `alembic.ini` existed — the image
  build would have failed outright.
- The `web` service ran `npm install && npm run dev` against an empty `frontend/` with no
  `package.json`.

**Fix — and a deliberate scope decision:** rather than half-build the migration layer to satisfy the
config, the agent removed the premature references and documented the deferral:

- compose `api` command no longer invokes alembic, with a comment that it returns in Phase 1 alongside
  the first model
- `Dockerfile` no longer copies `alembic.ini`, with a comment explaining that a migration runner with no
  models is "scaffolding for its own sake"
- `web` is retained in the compose file (it documents the target architecture) but carries a **NOT YET
  USABLE** comment, and the README instructs `docker compose up db api`

### 5.5 Suggestions the agent declined to act on

These are self-directed refusals — no external suggestion was made and then overridden. Recorded because
"declined to build speculative infrastructure" is a design decision worth surfacing.

| Temptation | Why declined |
| --- | --- |
| Add `alembic.ini` + `Base` + an empty migration to make compose happy | Scaffolding for its own sake; the first migration is meaningless without models (Phase 1) |
| Add a generic `Repository[T]` base class to reduce boilerplate | Couples the domain to SQLAlchemy's query API and invites business logic into repositories — see `SOLID.md` "rejected abstractions" |
| Add Redis/Celery for background investigations | `ADR-016`: measured cost of the sync path is seconds; two extra services is not justified yet |
| Add interfaces for every service and calculator | Misapplied LSP/ISP. Documented in `SOLID.md` §"Where I deliberately did NOT apply SOLID" |
| Build a real LLM provider adapter | OQ-08 is unanswered; the port and mock satisfy STK-06 without guessing a vendor |
| Implement a frontend scaffold to populate the empty `frontend/` directory | Out of Phase 0 scope; a stub UI would be noise |
| Write a CI pipeline | Deferred to Phase 5 and listed as such in the README |
| Seed the database with plausible-looking demo data | Needs the schema first; `FR-015` covers it in Phase 1 |
| `git init` and commit | Not requested. No commits were made |

---

## 6. Verification performed

Every command below was actually executed in this session. Results are transcribed, not predicted.

### Passed

| # | Command | Working dir | Result |
| --- | --- | --- | --- |
| 1 | `python -m venv .venv` | `backend/` | created, Python 3.10.9 |
| 2 | `.venv/Scripts/python.exe -m pip install -e ".[dev]"` | `backend/` | exit 0 |
| 3 | `.venv/Scripts/python.exe -m pytest` | `backend/` | **59 passed** (1 upstream Starlette deprecation warning) |
| 4 | `.venv/Scripts/python.exe -m ruff check app tests` | `backend/` | **All checks passed!** |
| 5 | `.venv/Scripts/python.exe -m ruff format --check app tests` | `backend/` | **17 files already formatted** |
| 6 | `.venv/Scripts/python.exe -m mypy app` | `backend/` | **Success: no issues found in 10 source files** (`strict = true`) |
| 7 | `uvicorn app.main:app --host 127.0.0.1 --port 8123` + `curl /api/v1/health` | `backend/` | **HTTP 200**, body `{"status":"ok","service":"resolveiq-api","version":"0.1.0","environment":"development","llm_provider":"mock",...}` |
| 8 | `curl /openapi.json` piped through a JSON parser | `backend/` | `title: ResolveIQ API`, `paths: ['/api/v1/health']` |
| 9 | `docker compose config --quiet` / `docker compose config --services` | repo root | exit 0; services `db`, `api`, `web` — **compose file is syntactically valid** |

### Failed

| # | Command | Result | Consequence |
| --- | --- | --- | --- |
| 10 | `docker compose up -d db api` | ❌ `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine ... The system cannot find the file specified` | **Docker daemon was not running. The Compose stack has never been started and the backend image has never been built.** |

### Not attempted (and therefore not claimed)

- Alembic migrations — no migrations exist yet
- Integration tests — no database was available; `tests/integration/` is empty
- Any frontend command — `frontend/` has no `package.json`
- `make <target>` — GNU Make is not installed on this machine
- Any deployment — nothing was deployed anywhere

### What the passing tests actually cover

59 tests, all unit-level, requiring no database and no network:

- **`tests/unit/test_money.py` (55 tests)** — float rejection (`TypeError` subclass), `bool` rejection,
  NaN/Infinity rejection, currency normalisation and validation, cross-currency arithmetic and ordering
  raising, rounding-mode differences at the half (`HALF_UP` vs `HALF_EVEN` vs `HALF_DOWN`),
  `quantise` immunity to a hostile ambient decimal context, sub-cent prices surviving to storage scale,
  allocation conserving every minor unit across six weight shapes including negatives, immutability and
  hashability, exact string formatting with no scientific notation.
- **`tests/unit/test_app_health.py` (4 tests)** — app constructs, `/health` returns 200 and reports
  `llm_provider: mock`, **a planted `LLM_API_KEY` and `DATABASE_URL` password do not appear in the
  response** (NEP-09 asserted, not assumed), comma-separated CORS parsing, mock provider is the default.

### Honest gaps a reviewer should know about

1. **The Docker stack is unverified.** Compose syntax is valid; nothing has been run. If the image fails
   to build, that is untested territory.
2. **No integration test exists**, so no database constraint in the design — least of all the partial
   unique index that is the backbone of NEP-05 — has been exercised. Those claims are currently
   *designs*, not verified behaviour.
3. **`Money` is verified; the pricing engine that will use it does not exist.** The most important
   property of `Money` is that it is *used* by billing code, which is untested because there is no
   billing code.
4. **The concurrency guarantee for duplicate adjustments is unproven.** It has a specific planned test
   (two threads, one barrier, one `201` and one `409`) that does not yet exist.
5. **Version pinning is loose.** Dependencies use `>=` floors, not a lockfile. The install resolved and
   passed on this machine, but a future install may pull different versions. `pip freeze` output was not
   captured or committed.

---

## 7. Files created or changed in Phase 0

**Created (20):**

```
README.md
AGENT_USAGE.md
.env.example
.gitignore
docker-compose.yml
Makefile
docs/SYSTEM_DESIGN.md
docs/SOLID.md
docs/ENGINEERING_DECISIONS.md
docs/INTERVIEW_GUIDE.md
docs/REQUIREMENTS_TRACEABILITY.md
backend/pyproject.toml
backend/Dockerfile
backend/app/__init__.py
backend/app/main.py
backend/app/config.py
backend/app/domain/__init__.py
backend/app/domain/money.py
backend/tests/unit/test_money.py
backend/tests/unit/test_app_health.py
```

Plus 11 empty package `__init__.py` files (`app/api`, `app/ports`, `app/adapters`, `app/services`,
`app/pricing`, `tests/*`, `scripts`) and the directory skeleton
(`backend/migrations/versions`, `backend/scripts`, `frontend/src`).

**Changed after initial write (11 edits):** `docs/SYSTEM_DESIGN.md` (TOC), `docs/REQUIREMENTS_TRACEABILITY.md`
(6 edits — section cross-references), `docs/ENGINEERING_DECISIONS.md` (1 — ADR-013 title),
`backend/app/domain/money.py` (5 — `allocate` rewrite, comparison dunders, allocation internals),
`backend/tests/unit/test_money.py` (3 — lint fixes), `docker-compose.yml` (2),
`backend/Dockerfile` (1).

**Not created, deliberately:** `.env` exists locally as a copy of `.env.example` for convenience and is
gitignored. No commits were made. `frontend/` contains only empty directories.

---

## 8. Process assessment

**What went well**

- Inspecting the environment first caught four constraints (Python 3.10, no `make`, no `uv`/`poetry`, empty
  directory) that changed concrete decisions rather than being noted and forgotten.
- Writing the traceability matrix *before* the design documents forced every document to reference stable
  IDs, which is how the broken cross-references were later detectable at all.
- Building `Money` first, with tests, was correct: the `allocate` bug was found in the first file written,
  while it was still cheap to fix.
- Running the linters I had configured turned four assumed-clean files into verified-clean ones.

**What went poorly**

- The TOC/body numbering mismatch in `SYSTEM_DESIGN.md` was a careless authoring error that would have
  shipped broken navigation in a document meant to be read under time pressure during an interview.
- The compose/Dockerfile references to non-existent `alembic` artefacts were the classic failure of writing
  configuration from intent rather than from the current tree. Only actually running the stack surfaced it.
- Four unnecessary `type: ignore` comments shipped because the type checker was configured but not run
  until after the file was written.

**What would be done differently**

- Write section anchors once and cross-check them mechanically (a link checker over the Markdown) rather
  than by eye.
- Grep the tree for every filename mentioned in config before claiming a stack works.
- Run `ruff format` and `mypy` as part of writing each file, not as a final gate.

**Note on scope discipline:** the brief explicitly said not to generate the entire application in one pass
and not to implement complete business logic. Two of my own defects (5.4) were *attempts* to make the
scaffold look more complete than it was. Both were corrected by removing work rather than adding it, which
is the right direction — but the instinct to make a scaffold look finished is worth naming, because it is
the same instinct that produces untested claims.
