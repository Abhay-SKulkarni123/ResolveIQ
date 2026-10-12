# Deploying ResolveIQ

This is the operational companion to [`README.md`](./README.md). It states plainly
what the current repository can be deployed as, how to run it, and what must be
fixed before it could face real users or real billing data.

> **Read this first — the readiness verdict.**
>
> **This build is a demo / evaluation deployment. It is not production ready.**
> It can be run for a reviewer, an interview panel, or a small trusted demo
> audience. It must **not** be exposed to the public internet, real customers, or
> genuine billing data.
>
> The two hard blockers are the same in every environment:
>
> 1. **There is no real authentication.** Identity is a spoofable request header.
> 2. **There is no real AI provider.** The only provider is a deterministic mock.
>
> The full list is in [Production blockers](#production-blockers) below.

---

## 1. Readiness classification

| Class | Definition | This build |
| --- | --- | --- |
| Local development ready | Runs from a checkout with a local database | **Yes** — verified |
| Demo / evaluation deployment ready | Run for a small trusted audience, no real data | **Yes** — see caveats |
| Production ready | Real users, real billing data, security and availability guarantees | **No** |

What *is* real and verified: the deterministic billing engine, the case lifecycle
(open → investigate → review → decide → reopen → audit), the evidence/fingerprint
model, PostgreSQL and MySQL persistence, the API surface, and the built frontend.

What is not: authentication, a hosted model provider, and any container run.

---

## 2. Prerequisites

- **Docker + Docker Compose** (for the container path), or
- **Python 3.10+** and **Node 20+** (for the non-container path)
- **A database**: PostgreSQL 16 (reference) or MySQL 8. SQLite is not supported.

The container images are built for Linux. The non-container commands in this file
are shown for Windows (the environment this was verified on); adapt paths on
macOS/Linux (`.venv/bin/python` instead of `.venv/Scripts/python.exe`).

---

## 3. Configuration

All configuration is read by the **backend only**. The frontend receives none of
it: it calls a relative `/api/v1`, so no host or secret is ever compiled into the
bundle. Copy the template and edit it:

```bash
cp .env.example .env          # .env is gitignored; never commit it
```

The important variables (see `.env.example` for the full, commented list):

| Variable | Purpose | Demo value |
| --- | --- | --- |
| `APP_ENV` | `development` \| `test` \| `production` | `production` in the demo overlay |
| `DATABASE_URL` | SQLAlchemy URL for the durable store | `postgresql+psycopg://…` |
| `TEST_DATABASE_URL` | **Disposable** DB for migration tests | derived from `DATABASE_URL` |
| `CASE_STORE` | `postgres` (durable) or `memory` (ephemeral) | `postgres` |
| `CORS_ORIGINS` | Exact browser origins allowed to call the API | the frontend origin |
| `LLM_PROVIDER` | Must be `mock` (only value implemented) | `mock` |
| `DEMO_IDENTITY_ENABLED` | Keeps the spoofable header identity | `true` (demo only) |

**Secrets.** Supply `DATABASE_URL` credentials, and `LLM_*` if a real provider is
ever added, through the platform's environment/secret store — never by committing
a `.env`, and never with a `VITE_` prefix (Vite inlines `VITE_*` into the browser
bundle at build time). `.env` and `.env.*` are gitignored; `.env.example` holds
placeholders only.

**Never point `TEST_DATABASE_URL` at data you care about.** The migration test
suite runs `alembic downgrade base`, which drops every table in that database.

---

## 4. Database setup

### PostgreSQL (reference)

```bash
docker compose up -d db
psql -h 127.0.0.1 -U postgres -f backend/scripts/create_dev_database.sql
```

### MySQL 8 (verified in this repository)

```sql
CREATE DATABASE resolveiq      CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE resolveiq_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'resolveiq'@'%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON resolveiq.*      TO 'resolveiq'@'%';
GRANT ALL PRIVILEGES ON resolveiq_test.* TO 'resolveiq'@'%';
```

Then set `DATABASE_URL` / `TEST_DATABASE_URL`, e.g.
`mysql+pymysql://resolveiq:<password>@localhost:3306/resolveiq`.

---

## 5. Migrations

Migrations are a **separate release step, run before the new version serves
traffic** — never on application start. The schema is at revision `0005`.

```bash
cd backend
alembic upgrade head        # apply
alembic current             # show the revision the database is on
```

Never run `alembic downgrade base` against anything but the disposable test
database. There is no data-reset step in any deploy command here, by design.

---

## 6. Running it

### 6a. Non-container (simplest, verified)

```bash
# backend
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
alembic upgrade head
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# frontend (a second terminal)
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to 127.0.0.1:8000
```

### 6b. Demo deployment (containers)

The demo overlay builds the backend image, runs the API from the **image** (no
bind mount, no hot reload), serves the compiled frontend from nginx, and does
**not** publish the database port to the host.

```bash
# 1. database up first
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build db

# 2. migrate, as its own step
docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm api alembic upgrade head

# 3. serve api + built frontend
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d api web-prod
# frontend on http://localhost:8080
```

The plain `docker-compose.yml` on its own is the **local development** stack
(hot reload, Vite dev server). Use it for day-to-day work:

```bash
docker compose up --build          # dev stack; frontend on :5173
```

> **The container path has never been executed in this repository.** No Docker
> daemon was available while it was written, and none was available when this
> document was written. `docker compose config` (and the merged overlay config)
> parses cleanly, but treat every container instruction above as **unverified**
> until you have run it once yourself and watched it come up.

---

## 7. Health checks and logs

- **Liveness:** `GET /api/v1/health` returns `{"status":"ok", …}`. It deliberately
  does **not** touch the database, so a database blip does not turn into a
  restart storm. Both images declare a healthcheck against it.
- **Readiness:** there is no separate readiness endpoint. Database reachability is
  reported per request: a call that needs the database returns `503
  DATABASE_UNAVAILABLE` if it is down.
- **Logs:** the API writes one structured line per request
  (`http.request.completed` with method, path, status, duration) plus lifecycle
  events (`case.opened`, `case.investigated`, …). Every response carries an
  `X-Request-ID` header; the same id is on every server-side line for that
  request, so a client can point at the exact lines that produced a response. The
  query string and request bodies are not logged.

```bash
docker compose logs -f api
```

---

## 8. TLS, CORS and network exposure

- **TLS is expected to terminate in front of the app** (reverse proxy, load
  balancer, or platform ingress). The app does not serve TLS itself. Do not expose
  the raw API or the database port directly to a network you do not control.
- **CORS** must list the exact frontend origin(s), comma-separated. The default is
  `http://localhost:5173`; the demo overlay sets `http://localhost:8080`. Never
  set it to `*` (credentials are enabled).
- **Database exposure:** the base compose file publishes `5432` for local
  convenience; the demo overlay removes it. Keep it unpublished in any real
  environment.
- **Trusted hosts:** there is currently no `TrustedHostMiddleware`, so the Host
  header is not validated. A production deploy should add it. (See blockers.)

---

## 9. Scaling and operation notes

- The API is stateless per request (one transaction per request, committed at the
  request edge), so it can be replicated behind a load balancer. The durable store
  is the database.
- The investigation provider is deterministic and in-process; runs complete before
  the response. There is no background worker or queue to operate.
- `CASE_STORE=memory` is an **explicit** choice for demos only: it is not durable
  and is lost on restart. It is never selected as a fallback.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `503 DATABASE_UNAVAILABLE` | API up, database down/unreachable | Check the database container/service and `DATABASE_URL` host/port |
| App starts but every case request fails | `CASE_STORE` misspelled | It must be exactly `postgres` or `memory`; a typo fails loudly at startup |
| Browser blocked by CORS | Frontend origin not in `CORS_ORIGINS` | Add the exact origin (scheme + host + port), comma-separated |
| `alembic` cannot find settings | `alembic.ini` / `migrations/` missing from the image | Rebuild; the Dockerfile copies them explicitly |
| Migration test drops your data | `TEST_DATABASE_URL` points at a real DB | It must be a disposable sibling database |
| `422 INVALID_USAGE_EVENT` on a timestamp | Naive `datetime` (no timezone) | Send an explicit offset, e.g. `2026-03-15T09:00:00+00:00` |
| `422 INVALID_TIMESTAMP` | Trailing `Z` on Python 3.10 | Use `+00:00` instead of `Z` |

---

## 11. Production blockers

These are not polish items; each is a reason this build cannot face real users or
real billing data as-is.

1. **Authentication and authorization.** Identity is `X-Actor-Id` /
   `X-Actor-Role` — unauthenticated, unsigned, trivially spoofable. The single
   role gate (the decision endpoint requiring `reviewer`) trusts this header.
   Required: real authentication (signed tokens/sessions with a server-verified
   principal), server-side authorization, an auditable real principal on every
   review and decision, and enforced separation of duties.
2. **AI provider.** `LLM_PROVIDER` is a `Literal["mock"]`; only the deterministic
   mock exists. A hosted provider needs the adapter plus the budget cap, response
   redaction and prompt-injection handling that go with it. Monetary figures must
   stay in the deterministic engine.
3. **Container path unverified.** The Dockerfiles and Compose files parse but have
   never been run in this repository.
4. **Trusted hosts.** No `TrustedHostMiddleware`; add an allowed-hosts list.
5. **API docs exposure.** `/docs` and `/openapi.json` are served regardless of
   `APP_ENV`. Restrict or disable them in production.
6. **TLS.** Must be terminated in front of the app.
7. **Operational hardening.** No rate limiting, no WAF, no backups/restore
   procedure, no alerting, and the demo identity cannot be turned into a real
   control by flipping a flag — `DEMO_IDENTITY_ENABLED=false` only stops the
   default from being applied.

See `README.md` §Security notes for the security posture of the code itself.
