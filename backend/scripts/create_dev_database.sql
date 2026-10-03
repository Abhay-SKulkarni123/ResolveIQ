-- ResolveIQ: dedicated development database role and databases.
--
-- Run this ONCE, as the PostgreSQL superuser, against the existing Windows
-- service on port 5432. It does not modify pg_hba.conf, does not reset any
-- existing password, and does not touch any other database.
--
--   psql -h 127.0.0.1 -p 5432 -U <superuser> -d postgres \
--        -v resolveiq_password='<password>' \
--        -f backend/scripts/create_dev_database.sql
--
-- Running it a second time is safe: every statement is conditional.
--
-- WHY THESE STATEMENTS
--
-- The role is deliberately NOT a superuser and cannot create databases or roles.
-- It owns exactly the two databases this project needs, which is the minimum that
-- lets `alembic upgrade head` and `alembic downgrade base` work: DDL on its own
-- tables. See docs/ENGINEERING_DECISIONS.md ADR-021.
--
-- Note on the shape of this script: the password is interpolated by psql into a
-- plain SQL statement and then quoted by format(%L), rather than being passed
-- into a DO $$ ... $$ block. psql does not substitute :'variables' inside a
-- dollar-quoted body, so the obvious DO-block version of this script would fail
-- with a syntax error. \gexec executes whatever the preceding query returns, which
-- keeps the password out of the SQL text and correctly escapes quotes in it.

\set ON_ERROR_STOP on

-- Fail early with a clear message rather than creating a role with an unusable
-- password, which would only surface later as an authentication failure.
\if :{?resolveiq_password}
\else
\echo 'ERROR: pass -v resolveiq_password=<password>'
\quit
\endif

-- 1. Create the login role, only if it does not already exist.
--
--    NOSUPERUSER      no server-wide power; cannot read another database's data
--    NOCREATEDB       cannot create or drop databases (tests use a pre-made one)
--    NOCREATEROLE     cannot create other roles
--    NOBYPASSRLS      cannot bypass row-level security
--    NOREPLICATION    cannot stream the database to a replica
--
--    %L renders the password as a correctly quoted SQL literal, so a password
--    containing a quote or a backslash is stored as given rather than truncated.
SELECT format(
           'CREATE ROLE resolveiq LOGIN PASSWORD %L '
           'NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
           :'resolveiq_password'
       )
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resolveiq')
\gexec

-- If the role already existed its password is left alone. Changing a credential
-- that is already in use somewhere else is not this script's decision to make.
SELECT CASE
           WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resolveiq')
           THEN 'role resolveiq exists; its password was not changed'
           ELSE 'role resolveiq created'
       END AS role_status;

-- 2. Development database, owned by the role so Alembic can manage its schema.
SELECT 'CREATE DATABASE resolveiq OWNER resolveiq'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'resolveiq')
\gexec

-- 3. Disposable database used only by the migration integration tests.
--    `alembic downgrade base` drops every table, so this must never hold real data.
SELECT 'CREATE DATABASE resolveiq_test OWNER resolveiq'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'resolveiq_test')
\gexec

-- 4. Report the result so the operator can confirm what now exists.
SELECT d.datname                  AS database,
       pg_get_userbyid(d.datdba) AS owner,
       has_database_privilege('resolveiq', d.datname, 'CONNECT') AS can_connect,
       has_database_privilege('resolveiq', d.datname, 'CREATE')  AS can_create_schema_objects
FROM pg_database d
WHERE d.datname IN ('resolveiq', 'resolveiq_test')
ORDER BY d.datname;

-- 5. Confirm the role really is unprivileged. Every column here should read false
--    except rolcanlogin.
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin
FROM pg_roles
WHERE rolname = 'resolveiq';

-- 6. Tell the operator what to do next, including where to put the password.
\echo ''
\echo 'Next: put this line in the repository-root .env (gitignored):'
\echo '  DATABASE_URL=postgresql+psycopg://resolveiq:<password>@localhost:5432/resolveiq'
\echo '  TEST_DATABASE_URL=postgresql+psycopg://resolveiq:<password>@localhost:5432/resolveiq_test'
\echo 'Then:  cd backend && pytest tests/integration'
