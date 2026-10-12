# Deploying ResolveIQ

This is the operational companion to [`README.md`](./README.md). It states plainly
what the current repository can be deployed as, how to run it, and what must still
be fixed before it could face real users or real billing data.

> **Read this first — the readiness verdict.**
>
> **This build is a demo / evaluation deployment. It is not production ready.**
> It can be run for a reviewer, an interview panel, or a small trusted demo
> audience. It must **not** be exposed to the public internet, real customers, or
> genuine billing data without the work in [Production blockers](#17-production-blockers).
>
> Two things that were hard blockers are now **implemented behind configuration**:
>
> 1. **Authentication.** `AUTH_MODE=token` verifies an HMAC-signed bearer token and
>    takes the role from the signed claims; production *refuses to start* unless it
>    is selected with a real secret. The spoofable header identity remains only as an
>    explicitly-labelled demo mode.
> 2. **AI provider.** An OpenAI-compatible HTTP adapter now exists beside the
>    deterministic mock. It has **not** been exercised against a live provider here
>    (no credentials), so it is verified against a stubbed transport, not in the field.
>
> What is *not* done is operational: the container path has never been run in this
> repository, TLS is still terminated in front, there is no observability backend, no
> backup that has been rehearsed, and no live-provider verification. The full list is
> in [Production blockers](#17-production-blockers).

---

## 1. Readiness classification

| Class | Definition | This build |
| --- | --- | --- |
| Local development ready | Runs from a checkout with a local database | **Yes** — verified |
| Demo / evaluation deployment ready | Run for a small trusted audience, no real data | **Yes** — see caveats |
| Production ready | Real users, real billing data, security and availability guarantees | **No** |

What *is* real and verified: the deterministic billing engine, the case lifecycle
(open → investigate → review → decide → reopen → audit), the evidence/fingerprint
model, PostgreSQL and MySQL persistence, the API surface, token-based authentication
and authorization, the API hardening (host allow-list, security headers, rate limit,
docs gating), and the built frontend.

What is not: a container run, a live model provider call, TLS termination,
observability, and a rehearsed backup/restore.

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
| `APP_ENV` | `development` \| `test` \| `production` | `development` in the demo overlay |
| `DATABASE_URL` | SQLAlchemy URL for the durable store | `postgresql+psycopg://…` |
| `TEST_DATABASE_URL` | **Disposable** DB for migration tests | derived from `DATABASE_URL` |
| `CASE_STORE` | `postgres` (durable) or `memory` (ephemeral) | `postgres` |
| `CORS_ORIGINS` | Exact browser origins allowed to call the API | the frontend origin |
| `ALLOWED_HOSTS` | Host header allow-list (`TrustedHostMiddleware`) | `*` in dev; concrete in prod |
| `EXPOSE_DOCS` | Swagger/ReDoc + `/openapi.json`: empty/`auto` = on in dev, off in prod | *(empty)* |
| `SECURITY_HEADERS_ENABLED` | nosniff / frame-deny / referrer-policy headers | `true` |
| `RATE_LIMIT_ENABLED` | Fixed-window limiter (always on in production) | `false` in dev |
| `AUTH_MODE` | `demo` (spoofable headers) or `token` (verified bearer) | `demo`; prod requires `token` |
| `AUTH_SECRET` | HS256 signing secret for token mode | *(empty)*; prod requires ≥16 |
| `LLM_PROVIDER` | `mock` or `openai` (OpenAI-compatible HTTP) | `mock` |
| `LLM_BASE_URL` / `LLM_API_KEY` | Endpoint and key for the OpenAI-compatible adapter | api.openai.com / *(empty)* |

**Secrets.** Supply `DATABASE_URL` credentials, `AUTH_SECRET` and `LLM_API_KEY`
through the platform's environment/secret store — never by committing a `.env`, and
never with a `VITE_` prefix (Vite inlines `VITE_*` into the browser bundle at build
time). `.env` and `.env.*` are gitignored; `.env.example` holds placeholders only.

**Never point `TEST_DATABASE_URL` at data you care about.** The migration test
suite runs `alembic downgrade base`, which drops every table in that database.

**Production refuses unsafe values at startup.** With `APP_ENV=production` the
application raises rather than booting if `AUTH_MODE` is not `token`, if
`AUTH_SECRET` is shorter than 16 characters, or if `CORS_ORIGINS` or `ALLOWED_HOSTS`
still contains `*`. This is deliberate: a deployment that boots with spoofable
identity or a wildcard origin *looks* healthy and is not.

---

## 4. Authentication and authorization

Two modes, chosen by `AUTH_MODE`. Every route resolves one `Actor` either way, and
the role is consulted in exactly one place: the decision endpoint insists on
`reviewer` (`app/domain/identity.py::can_decide`), because a closing verdict
permanently changes the lifecycle. `GET /api/v1/capabilities` always reports which
identity mode is live and whether the identity was verified, so a consumer can never
read a declared identity as a verified one.

### `AUTH_MODE=demo` (development and the container demo)

The `X-Actor-Id` / `X-Actor-Role` request headers. **These are trivially spoofable
and are not authentication.** They exist so the review/approve flow can be
demonstrated without a login step. `DEMO_IDENTITY_ENABLED=false` only stops the
development default from being applied; it does not add any verification.
`APP_ENV=production` refuses this mode.

### `AUTH_MODE=token` (production)

A verified `Authorization: Bearer <HS256 JWT>`. The adapter pins the algorithm to
`HS256`, verifies the signature over the received segments, checks `iss`, `sub`,
`exp` (with a leeway), and takes the role from the signed claims. Failures use stable
codes: `UNAUTHENTICATED` (no token), `INVALID_CREDENTIALS` (bad signature/shape),
`CREDENTIALS_EXPIRED` (past `exp`), `INVALID_IDENTITY` (role/subject/issuer not
acceptable).

Mint a token with the operator CLI (it reads `AUTH_SECRET` from config/environment
and never from the command line):

```bash
cd backend
.venv/Scripts/python.exe -m scripts.issue_token --subject alice --role reviewer
# --role must be one of: analyst | reviewer ; --ttl overrides AUTH_TOKEN_TTL_SECONDS
```

The frontend sends the token when one is present. There is **no login UI**; the
workbench bootstraps a token from `localStorage` under the key `resolveiq.authToken`
(`src/main.tsx`). An operator (or a hosting page) places a token there out-of-band:

```js
localStorage.setItem('resolveiq.authToken', '<token from scripts/issue_token>')
```

A deployment that wants real end-user login should replace this with an auth
provider; the seam is the single `setAuthToken()` call in `src/api/client.ts`.

> **Tokens are bearer credentials.** Anyone holding one acts as that subject until
> it expires. Serve over TLS only, keep `AUTH_TOKEN_TTL_SECONDS` short, and treat
> `AUTH_SECRET` as a root secret (rotating it invalidates every outstanding token).

---

## 5. AI provider

`LLM_PROVIDER` selects the interpretation provider behind the `LlmProvider` port:

- `mock` (default) — deterministic, offline, used by the tests and by the demo. No
  network, no key.
- `openai` — the `OpenAiCompatibleProvider` adapter (`app/adapters/llm/openai_compatible.py`),
  a synchronous `httpx` client against any OpenAI-compatible chat-completions
  endpoint (`LLM_BASE_URL`). Requires `LLM_API_KEY`; the process refuses to start
  without it rather than silently answering from the mock.

Either way the **deterministic engine remains the sole authority for money**, and the
model's response schema has no field capable of holding a monetary amount.

> **Not verified against a live provider.** No provider credentials were available
> in this environment, so the adapter is exercised against an `httpx.MockTransport`
> in the contract/unit suites only. Before relying on it, run one investigation
> against your chosen endpoint and confirm the parse, the timeout path and the
> retry behaviour.

---

## 6. Database setup

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

## 7. Migrations

Migrations are a **separate release step, run before the new version serves
traffic** — never on application start. The schema is at revision `0005`.

```bash
cd backend
alembic upgrade head        # apply
alembic current             # show the revision the database is on
alembic heads               # show the head revision(s)
```

Never run `alembic downgrade base` against anything but the disposable test
database. There is no data-reset step in any deploy command here, by design.

---

## 8. Running it

### 8a. Non-container (simplest, verified)

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

### 8b. Demo deployment (containers)

The demo overlay builds the backend image, runs the API from the **image** (no
bind mount, no hot reload), serves the compiled frontend from nginx, and does
**not** publish the database port to the host. It runs `APP_ENV=development` with
`AUTH_MODE=demo` on purpose, so the browser demo needs no login; it is a demo, not a
production profile.

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

## 9. Health checks, logs and monitoring

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

**There is no metrics endpoint and no alerting wired up.** A production rollout must
add both. The signals to alert on, all derivable from the structured log stream:

| Signal | Threshold to start from | Why it matters |
| --- | --- | --- |
| 5xx rate | > 1% of requests over 5 min | Users are seeing failures |
| `503 DATABASE_UNAVAILABLE` | any sustained occurrence | The durable store is unreachable; cases cannot be opened or read |
| 429 rate | sustained above a small baseline | Either abuse or a rate limit set too low for real traffic |
| Request duration p99 | > 2 s | The provider call is on the request path; a slow endpoint slows every investigation |
| Auth failures (`UNAUTHENTICATED`/`INVALID_CREDENTIALS`) | spike | Credential stuffing, or a token/signing-secret mismatch after a deploy |

---

## 10. TLS, CORS, hosts and network exposure

- **TLS is expected to terminate in front of the app** (reverse proxy, load
  balancer, or platform ingress). The app does not serve TLS itself. Do not expose
  the raw API or the database port directly to a network you do not control.
- **CORS** must list the exact frontend origin(s), comma-separated. The default is
  `http://localhost:5173`; the demo overlay sets `http://localhost:8080`. Never
  set it to `*` (credentials are enabled), and production refuses `*` at startup.
- **Trusted hosts:** `ALLOWED_HOSTS` is enforced by `TrustedHostMiddleware`, so a
  request with a Host header not in the list is rejected. Production refuses `*`.
  Behind a proxy, list the public hostname(s) the browser uses.
- **Client IP behind a proxy:** uvicorn reads `FORWARDED_ALLOW_IPS` to decide which
  peers may set `X-Forwarded-*` (used for the rate-limit key and logs). Set it to the
  proxy's address; it is a uvicorn setting, not an application setting, so it is not
  read from `.env`.
- **Database exposure:** the base compose file publishes `5432` for local
  convenience; the demo overlay removes it. Keep it unpublished in any real
  environment.

---

## 11. API hardening

| Control | Toggle | Behaviour |
| --- | --- | --- |
| Security headers | `SECURITY_HEADERS_ENABLED` (default on) | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin` |
| Rate limiting | `RATE_LIMIT_ENABLED` (always on in production) | Fixed window, per client host, `RATE_LIMIT_REQUESTS`/`RATE_LIMIT_WINDOW_SECONDS` (120/60), `429 RATE_LIMITED` with `Retry-After`; `/api/v1/health` and `OPTIONS` are exempt |
| Host allow-list | `ALLOWED_HOSTS` | Rejects an unexpected Host header |
| API docs | `EXPOSE_DOCS` (empty/`auto` = on in dev, off in prod) | Swagger, ReDoc and `/openapi.json` are all withheld together in production |

The limiter is **per process**. Behind more than one replica or a load balancer it
is a coarse per-instance cap, not a global quota; a real deployment should rate-limit
at the edge as well.

---

## 12. Backup and restore

**Not rehearsed here** — these are the standard procedures for the two supported
engines, to be tested against your own infrastructure before you rely on them.

### PostgreSQL

```bash
# Logical backup (custom format, restorable selectively)
pg_dump -Fc -h <host> -U resolveiq resolveiq > resolveiq-$(date +%F).dump

# Restore into an empty database of the same name
createdb -h <host> -U <owner> resolveiq_restore
pg_restore -h <host> -U <owner> -d resolveiq_restore resolveiq-$(date +%F).dump
```

### MySQL 8

```bash
mysqldump --single-transaction --routines --triggers \
  -h <host> -u resolveiq -p resolveiq > resolveiq-$(date +%F).sql

# Restore into an empty database
mysql -h <host> -u resolveiq -p resolveiq_restore < resolveiq-$(date +%F).sql
```

**Restore and the schema version.** The dump captures `alembic_version`. Restoring
an older dump onto a database that has already been migrated forward restores the old
revision too, so the next deploy's `alembic upgrade head` re-applies the migrations
that were skipped. Always restore into a database you have emptied, and run
`alembic current` after restoring to confirm the revision matches the code.

**What must not be lost.** The database is the only durable store; `CASE_STORE=memory`
is explicitly not durable. The audit trail is append-only and is your record of who
decided what — it is the part of a backup you can least afford to lose.

---

## 13. Scaling and operation notes

- The API is stateless per request (one transaction per request, committed at the
  request edge), so it can be replicated behind a load balancer. The durable store
  is the database.
- The investigation provider runs on the request path and completes before the
  response. With `openai`, that adds a network round-trip (bounded by
  `LLM_TIMEOUT_SECONDS`) to every investigation. There is no background worker or
  queue to operate.
- `CASE_STORE=memory` is an **explicit** choice for demos only: it is not durable
  and is lost on restart. It is never selected as a fallback.

---

## 14. Migration rollout and rollback

**Forward (the normal path).** Run `alembic upgrade head` as its own step *before*
the new application version serves traffic. The migrations are additive and the
schema is at `0005`; `alembic current` confirms where a database is.

**Application rollback.** Re-deploy the previous image/commit. The application does
not migrate on start, so rolling the application back does not touch the schema.

**Schema rollback.** `alembic downgrade <revision>` is implemented and is exercised
in CI for the full up/down cycle, but treat it as a break-glass step: a downgrade
that drops a column discards that column's data. For a production incident the safer
rollback is usually **restore from the last good backup** (§12) rather than a
downgrade. Never run `downgrade base` outside the disposable test database.

---

## 15. Production checklist

Before exposing this to real users or real billing data, every box must be ticked and
the result recorded — "it started" is not verification.

- [ ] `APP_ENV=production`, `AUTH_MODE=token`, and a strong `AUTH_SECRET` (32+
      random bytes) from a secret store — the app refuses to start otherwise.
- [ ] `CORS_ORIGINS` and `ALLOWED_HOSTS` list the real hostname(s); no `*`.
- [ ] TLS terminates in front of the API and the frontend; database port unpublished.
- [ ] Token issuance is integrated with your real identity source, and the frontend
      bootstrap is replaced by that login flow (or tokens are provisioned deliberately).
- [ ] `LLM_PROVIDER` and `LLM_API_KEY` set, with one investigation verified end to
      end against the chosen endpoint, and a spend cap at the provider.
- [ ] Migrations run as a release step; `alembic current` matches the deployed code.
- [ ] A backup has been taken **and restored into a scratch database successfully**.
- [ ] Logs are shipped somewhere durable; alerts from §9 are configured.
- [ ] The `docker compose` demo path (or your own images) has been built and run once.
- [ ] `/health` returns `ok` and an authenticated `POST /disputes` succeeds through
      the proxy.

---

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| App refuses to start with a production message | `APP_ENV=production` and a demo/wildcard/weak value | Set `AUTH_MODE=token` + `AUTH_SECRET`, concrete `CORS_ORIGINS`/`ALLOWED_HOSTS` |
| `401 UNAUTHENTICATED` in token mode | No `Authorization: Bearer` header | Send a token from `scripts/issue_token` |
| `401 CREDENTIALS_EXPIRED` | Token past `exp` (minus leeway) | Mint a fresh token; check `AUTH_TOKEN_TTL_SECONDS` |
| `401 INVALID_CREDENTIALS` | Wrong `AUTH_SECRET` / malformed token | Re-issue with the same secret the server holds |
| `400` for an unexpected Host | Host not in `ALLOWED_HOSTS` | Add the real hostname (or set it for the proxy) |
| `429 RATE_LIMITED` | Limiter tripped | Raise `RATE_LIMIT_REQUESTS`/window, or rate-limit at the edge |
| `503 DATABASE_UNAVAILABLE` | API up, database down/unreachable | Check the database container/service and `DATABASE_URL` host/port |
| App starts but every case request fails | `CASE_STORE` misspelled | It must be exactly `postgres` or `memory`; a typo fails loudly at startup |
| Browser blocked by CORS | Frontend origin not in `CORS_ORIGINS` | Add the exact origin (scheme + host + port), comma-separated |
| `alembic` cannot find settings | `alembic.ini` / `migrations/` missing from the image | Rebuild; the Dockerfile copies them explicitly |
| Migration test drops your data | `TEST_DATABASE_URL` points at a real DB | It must be a disposable sibling database |
| `422 INVALID_USAGE_EVENT` on a timestamp | Naive `datetime` (no timezone) | Send an explicit offset, e.g. `2026-03-15T09:00:00+00:00` |
| `422 INVALID_TIMESTAMP` | Trailing `Z` on Python 3.10 | Use `+00:00` instead of `Z` |

---

## 17. Production blockers

These are not polish items; each is a reason this build cannot face real users or
real billing data as-is. (Authentication and the provider adapter are *implemented*;
what remains for them is integration and field verification, noted below.)

1. **Container path unverified.** The Dockerfiles and Compose files parse but have
   never been run in this repository. Build and run them once before trusting them.
2. **Live provider unverified.** The OpenAI-compatible adapter has never been run
   against a hosted endpoint here; it is tested against a stubbed transport only.
   Verify a real call, a timeout, and a retry, and set a provider spend cap.
3. **TLS not provided by the app.** Must be terminated in front; the app serves
   plain HTTP.
4. **No observability backend.** Structured logs exist, but there is no metrics
   endpoint, no log shipping and no alerting configured. §9 lists the signals.
5. **No rehearsed backup/restore.** §12 gives the procedure; it has not been run
   against real infrastructure.
6. **Identity integration.** Token verification is real, but issuance is a manual
   operator CLI and the frontend bootstrap reads `localStorage`. A production system
   needs issuance integrated with the real identity source and a login flow.
7. **In-process rate limiting.** Fine for a single instance; add edge limiting for
   any multi-replica deployment.
8. **No money-moving path.** Adjustments/approvals are deliberately out of scope;
   the decision endpoint records a verdict and moves no money.

See `README.md` §8 for the security posture of the code itself.

---

## 18. Continuous integration

`.github/workflows/ci.yml` runs three jobs on push to `main` and on pull requests:

| Job | What it runs |
| --- | --- |
| Backend (unit + contract) | `ruff check`, `mypy app`, `pytest tests/unit tests/contract` |
| Backend (PostgreSQL integration) | `pytest tests/integration` against a `postgres:16` service, with the disposable `resolveiq_test` database created first |
| Frontend | `npm ci`, `npm run lint`, `npm run typecheck`, `npm test`, `npm run build` |

The integration job is the only place the migration up/down cycle is exercised
against **PostgreSQL**; locally the same suite is frequently run against MySQL, so a
green local run is not a substitute for the CI job.
