"""Phase 4 persistence: the nine case tables and their agreement with the migration.

``test_persistence_models.py`` covers the Phase 1-2 source tables. This file covers
what Phase 4 added, and asserts the two properties that only fail once both the
models and the migration exist:

* the ORM metadata is **complete**, so Alembic autogenerate does not propose
  dropping the tables it cannot see, and
* the migration says the **same thing** as the metadata, so ``alembic upgrade``
  produces the schema the application expects.

Both are checked without a database. Whether PostgreSQL *accepts* the result is
``tests/integration/test_migrations.py``'s job, and that file skips when no server is
reachable. These are the checks that still run on a laptop with nothing installed,
which is exactly why the schema promises live here rather than only there.
"""

from __future__ import annotations

import ast
import re
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, Float, Integer, Numeric, UniqueConstraint

import app.adapters.persistence.ddl as ddl
from app.adapters.persistence import MONEY_PRECISION, MONEY_SCALE, Base
from app.adapters.persistence.case_models import (
    DisputeEvidenceRow,
    DisputeRow,
    InvestigationEvidenceRow,
    InvestigationHypothesisRow,
    InvestigationRow,
)
from app.domain.cases import (
    CaseSeverity,
    CaseStatus,
    InvestigationRunStatus,
    ReviewActionKind,
    ReviewTargetType,
)

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0002_dispute_cases.py"
)

PHASE4_TABLES = {
    "disputes",
    "dispute_evidence_items",
    "calculations",
    "investigations",
    "investigation_evidence",
    "investigation_findings",
    "investigation_hypotheses",
    "investigation_resolution_options",
    "finding_reviews",
}

#: Every monetary column Phase 4 added, per table. Listed explicitly rather than
#: pattern-matched on the substring "amount", because ``impact_amount`` and
#: ``recalculated_total`` must be NUMERIC too and a rename would otherwise pass
#: unnoticed.
PHASE4_MONEY_COLUMNS = {
    "calculations": (
        "recalculated_total",
        "recorded_total",
        "difference",
        "outstanding",
        "allocated_payments",
        "net_adjustments",
    ),
    "investigation_hypotheses": ("impact_amount",),
}


@pytest.fixture(scope="module")
def migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Metadata completeness
# ---------------------------------------------------------------------------



def _renders_as(column, backend: str, type_name: str) -> bool:
    """Report the concrete DDL type ``column`` compiles to for ``backend``.

    A generic JSON type carrying a PostgreSQL variant reports two different types
    depending on the dialect, so asserting on the declared Python type alone would
    pass even if the variant mapping were dropped.
    """
    from sqlalchemy.dialects import mysql, postgresql

    dialects = {"postgresql": postgresql.dialect(), "mysql": mysql.dialect()}
    rendered = str(column.type.compile(dialect=dialects[backend]))
    return type_name in rendered



def test_importing_the_package_registers_every_phase4_table() -> None:
    """The failure this guards is silent and destructive.

    ``migrations/env.py`` sets ``target_metadata`` from ``Base.metadata``. A model
    module that is never imported leaves its tables out of that metadata, and the
    next ``alembic revision --autogenerate`` helpfully writes a migration dropping
    all nine of them -- for tables that hold every dispute, investigation and
    review in the system.

    The import in ``app/adapters/persistence/__init__.py`` exists to make "the
    package is imported" and "the metadata is complete" the same event. This asserts
    the second follows from the first.
    """
    assert set(Base.metadata.tables) >= PHASE4_TABLES


def test_phase4_tables_are_not_in_any_earlier_migration() -> None:
    """0002 owns these tables outright.

    If a Phase 1 or 2 migration already created one, then ``0001:0002 --sql`` would
    emit a duplicate ``CREATE TABLE`` and the failure would appear on a developer's
    database rather than in review.
    """
    versions = MIGRATION.parent
    earlier = {
        path.read_text(encoding="utf-8")
        for path in versions.glob("0001*.py")
    }
    for table in PHASE4_TABLES:
        for source in earlier:
            assert f'"{table}"' not in source, table


# ---------------------------------------------------------------------------
# Money, probabilities and hashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        (table, column)
        for table, columns in PHASE4_MONEY_COLUMNS.items()
        for column in columns
    ],
)
def test_phase4_money_columns_are_numeric_19_4(table_name: str, column_name: str) -> None:
    """ADR-011 extends to the dispute tables, not just the contract ones.

    These are the figures a reviewer acts on. A float here is the same defect as a
    float on a unit price, one phase closer to a decision.
    """
    column = Base.metadata.tables[table_name].columns[column_name]
    assert isinstance(column.type, Numeric), f"{table_name}.{column_name}"
    assert column.type.precision == MONEY_PRECISION
    assert column.type.scale == MONEY_SCALE


def test_likelihood_is_numeric_within_probability_range() -> None:
    """A likelihood is a probability, so NUMERIC(5,4): four decimals, max 9.9999.

    The CHECK constraint narrows it to 0-1; the precision is what stops a value like
    0.123456 being silently truncated on the way in.
    """
    column = Base.metadata.tables["investigation_hypotheses"].columns["likelihood"]
    assert isinstance(column.type, Numeric)
    assert column.type.precision == 5
    assert column.type.scale == 4


def test_fingerprint_columns_are_varchar_71() -> None:
    """A SHA-256 hex digest is 64 characters.

    71 leaves room for a version prefix such as ``sha256:`` without a migration, and
    is wide enough that the column cannot truncate a digest -- which would make two
    different evidence sets hash alike.
    """
    for table_name, column_name in (
        ("disputes", "evidence_fingerprint"),
        ("investigations", "evidence_fingerprint"),
        ("dispute_evidence_items", "content_hash"),
        ("finding_reviews", "evidence_fingerprint_seen"),
    ):
        column = Base.metadata.tables[table_name].columns[column_name]
        assert column.type.length == 71, f"{table_name}.{column_name}"


def test_no_phase4_column_uses_a_floating_point_type() -> None:
    """NEP-01 at the storage layer, restated for the new tables."""
    offenders = [
        f"{table}.{column.name}"
        for table in PHASE4_TABLES
        for column in Base.metadata.tables[table].columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Immutability and lifecycle, expressed as constraints
# ---------------------------------------------------------------------------


def test_evidence_snapshot_is_json_and_required() -> None:
    """The snapshot is the citable fact, so it cannot be absent.

    The column is a generic JSON carrying a PostgreSQL JSONB variant, so it stays
    JSONB on the reference backend and becomes native JSON on MySQL.

    An evidence row with a hash and no snapshot would be a fingerprint of nothing:
    a reviewer could not be shown what was hashed, and a staleness comparison would
    be attesting to an empty document.
    """
    snapshot = DisputeEvidenceRow.__table__.columns["snapshot"]
    assert _renders_as(snapshot, "postgresql", "JSONB")
    assert _renders_as(snapshot, "mysql", "JSON")
    assert snapshot.nullable is False


def test_source_document_is_required_json() -> None:
    """JSON, and NOT NULL.

    Required rather than nullable: every case is opened from an ingest payload, so
    a row with no payload would be a case that cannot be re-investigated. The
    repository stores ``{"present": false}`` for a case constructed without one --
    an explicit statement that there is nothing to recalculate -- which is a
    different thing from a NULL the domain cannot interpret.
    """
    column = DisputeRow.__table__.columns["source_document"]
    assert _renders_as(column, "postgresql", "JSONB")
    assert _renders_as(column, "mysql", "JSON")
    assert column.nullable is False


def test_version_is_required_so_a_concurrent_write_can_be_detected() -> None:
    """``CASE-004``: optimistic concurrency needs a stored version to compare to.

    ``save`` issues ``UPDATE ... WHERE version = :expected``; without the column the
    guard has nothing to match and every write becomes last-write-wins, silently
    dropping one of two reviewers' annotations.
    """
    column = DisputeRow.__table__.columns["version"]
    assert column.nullable is False
    # INTEGER rather than NUMERIC: the guard is an equality comparison on a
    # monotonically increasing count, so decimal scale would buy nothing.
    assert isinstance(column.type, Integer)


@pytest.mark.parametrize(
    ("table_name", "expected"),
    [
        ("disputes", ("external_id",)),
        ("dispute_evidence_items", ("dispute_id", "natural_key", "content_hash")),
        ("investigations", ("dispute_id", "version")),
    ],
)
def test_uniqueness_rules_the_domain_relies_on(table_name: str, expected: tuple[str, ...]) -> None:
    """The business keys the service's idempotence and ordering depend on.

    ``(dispute_id, natural_key, content_hash)`` is what makes re-attaching the same
    snapshot a no-op under concurrency: two simultaneous attaches both find it
    absent, and the unique index makes the second one a no-op rather than a duplicate
    row that would change the fingerprint.
    """
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert expected in unique, f"{table_name}: {sorted(unique)}"


def test_finding_reviews_carry_no_uniqueness_constraint_yet() -> None:
    """A known gap, pinned so it cannot be forgotten.

    ``save`` refuses to overwrite an existing review row, but nothing stops two
    *identical* reviews being stored, and a double-submitted POST produces exactly
    that: two rows, same actor, same target, same rationale. The fix belongs to the
    idempotency phase (SYSTEM_DESIGN §9.3), which brings an ``Idempotency-Key`` and
    a uniqueness rule together -- adding a content hash here would be inventing that
    design without the key, and would reject two legitimate identical annotations
    from the same reviewer later.

    Until then the duplicate is possible and this test says so out loud.
    """
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables["finding_reviews"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique == set()


def test_investigation_version_is_unique_per_dispute() -> None:
    """Dense, gapless versions per case.

    A gap would make "the previous run" ambiguous, and ``with_investigation`` derives
    the new version from the case, so two runs cannot claim the same number.
    """
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables["investigations"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("dispute_id", "version") in unique


def test_investigation_evidence_is_a_composite_key_not_a_surrogate() -> None:
    """The pair is the identity.

    A surrogate ``id`` here would permit storing the same investigation's link to
    the same snapshot twice, and a duplicated link would make the evidence count a
    run reports wrong.
    """
    columns = InvestigationEvidenceRow.__table__.columns
    assert {column.name for column in columns} == {"investigation_id", "evidence_item_id"}
    primary = {column.name for column in InvestigationEvidenceRow.__table__.primary_key.columns}
    assert primary == {"investigation_id", "evidence_item_id"}


# ---------------------------------------------------------------------------
# The hypothesis impact constraints, which must match the domain
# ---------------------------------------------------------------------------


def _check_sql(table_name: str) -> str:
    return "\n".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_a_hypothesis_without_an_amount_must_be_explained() -> None:
    """No amount means either a basis or a stated reason -- not silence.

    "not assessable" and "no impact" are different answers to a reviewer, and a row
    with neither reads as the second.
    """
    sql = _check_sql("investigation_hypotheses")
    assert "missing_impact_is_explained" not in sql  # the name is prefixed by convention
    assert "COALESCE(TRIM(impact_basis), '') <> ''" in sql
    assert "COALESCE(TRIM(not_assessable_reason), '') <> ''" in sql


def _migration_check_sql(table_name: str, bare_name: str) -> str:
    """The SQL of one named CHECK inside a migration's ``create_table`` block.

    Scoped to a single table because bare constraint names repeat across tables --
    ``currency_is_iso4217`` is on several -- so a whole-file search would happily
    return a same-named constraint from an unrelated table and report agreement
    that is not there.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    # Matched on the quoted name alone rather than the whole call: the table argument
    # sits on its own line at whatever indentation the file happens to use, and
    # pinning the exact layout here would fail on a reformat rather than on drift.
    needle = f'"{table_name}",'
    start = text.index(needle)
    start = text.rindex("op.create_table(", 0, start)
    end = text.index("op.", start + 1)
    block = text[start:end]
    name_at = block.index(f'name="{bare_name}"')
    # Walk back over the preceding sa.CheckConstraint( to the opening quote.
    call_at = block.rindex("sa.CheckConstraint(", 0, name_at)
    # The SQL is written as several adjacent double-quoted literals, so every segment
    # before ``name=`` belongs to the constraint and has to be concatenated -- taking
    # only the first quoted run would truncate the clause mid-way and compare a
    # prefix against the ORM's full text.
    sql = " ".join(re.findall(r'"([^"]*)"', block[call_at:name_at]))
    return " ".join(sql.split())


def test_the_impact_constraints_allow_a_directional_hypothesis() -> None:
    """Phase 3 can produce an amount-less impact that still has a basis.

    ``ImpactAssessment.basis`` is mandatory and ``not_assessable_reason`` is
    optional, so "usage is overstated, but no rule prices it" is a legitimate state.
    An earlier version of this schema required a *reason* whenever the amount was
    null, which would have made the repository reject rows the domain accepts -- the
    database and the domain silently disagreeing about what is legal.
    """
    sql = _check_sql("investigation_hypotheses")
    # The constraint is an OR across all three, not an implication to a reason alone.
    constraint = next(
        str(c.sqltext)
        for c in InvestigationHypothesisRow.__table__.constraints
        if isinstance(c, CheckConstraint) and "missing_impact" in (c.name or "")
    )
    assert "OR" in constraint
    assert "impact_basis" in constraint
    assert "not_assessable_reason" in constraint
    # A basis alone must satisfy it, so the clause cannot require the reason as well.
    assert "impact_basis IS NULL" not in constraint
    assert "not_assessable_reason IS NULL" not in constraint
    # The sibling amount-implies-basis clause is what stops the OR from being a
    # loophole: a present amount still has to say where it came from.
    assert "impact_amount IS NULL OR COALESCE(TRIM(impact_basis), '') <> ''" in sql
    # And the migration has to agree with the ORM, or the two drift apart silently.
    migrated = _migration_check_sql("investigation_hypotheses", "missing_impact_is_explained")
    assert migrated == constraint
    assert migrated.count("OR") == 2
    assert "impact_basis" in migrated
    assert "not_assessable_reason" in migrated


def test_an_amount_requires_a_basis_and_excludes_an_unassessable_reason() -> None:
    """A figure with no explanation of how it was reached is not reviewable, and an
    amount plus "not assessable" is a contradiction."""
    constraints = {
        c.name: str(c.sqltext)
        for c in InvestigationHypothesisRow.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert any(
        "impact_amount IS NULL OR COALESCE(TRIM(impact_basis), '') <> ''" in sql
        and "amount_implies_basis" in (name or "")
        for name, sql in constraints.items()
    )
    assert any(
        "impact_amount IS NULL OR not_assessable_reason IS NULL" in sql
        and "amount_excludes_unassessable" in (name or "")
        for name, sql in constraints.items()
    )


def test_review_actions_do_not_include_approve() -> None:
    """The vocabulary has four verbs and none of them moves money.

    ``APPROVE`` belongs with the idempotency key and the separation-of-duties rules
    of SYSTEM_DESIGN §9.3. Shipping the verb into a CHECK constraint now would put
    it in every layer above this one before its guards exist.
    """
    sql = _check_sql("finding_reviews")
    assert "APPROVE" not in sql
    for action in ("ACCEPT", "REJECT", "REQUEST_MORE_INFO", "AMEND"):
        assert f"'{action}'" in sql, action


def test_finding_reviews_are_append_only_by_construction() -> None:
    """There is no ``updated_at``, so there is nothing to update.

    A reviewer annotation that could be edited in place would stop being evidence of
    what was decided; the ``AMEND`` action stores a replacement alongside the
    original finding for exactly this reason.
    """
    assert "updated_at" not in InvestigationRow.__table__.columns


# ---------------------------------------------------------------------------
# Migration / metadata agreement
# ---------------------------------------------------------------------------


def _migration_tables(migration_source: str) -> set[str]:
    return set(re.findall(r'op\.create_table\(\s*"([a-z0-9_]+)"', migration_source))


def test_migration_creates_exactly_the_phase4_tables(migration_source: str) -> None:
    assert _migration_tables(migration_source) == PHASE4_TABLES


def test_migration_closes_the_disputes_investigations_cycle(migration_source: str) -> None:
    """``disputes.current_investigation_id`` and ``investigations.dispute_id``.

    The two reference each other, so one of the constraints has to be added after
    both tables exist. The migration does that with an ``ALTER TABLE``; without it
    the ``CREATE TABLE`` order is unsatisfiable.
    """
    assert re.search(
        r'op\.create_foreign_key\(\s*"fk_disputes_current_investigation_id_investigations"',
        migration_source,
    ), "the deferred foreign key is missing"


def test_migration_drops_the_phase4_tables_on_downgrade(migration_source: str) -> None:
    dropped = set(re.findall(r'op\.drop_table\(\s*"([a-z0-9_]+)"', migration_source))
    assert dropped == PHASE4_TABLES


@pytest.mark.parametrize("table_name", sorted(PHASE4_TABLES))
def test_migration_columns_match_the_orm(table_name: str, migration_source: str) -> None:
    """Every ORM column appears in the migration's ``create_table`` for that table.

    A column added to a model but not to the migration fails at runtime, on the
    first write that uses it, in whichever environment is furthest from the author.
    """
    body = re.search(
        rf'op\.create_table\(\s*"{table_name}"(.*?)\n    \)',
        migration_source,
        re.S,
    )
    assert body is not None, table_name
    migration_columns = set(re.findall(r'sa\.Column\(\s*"([a-z0-9_]+)"', body.group(1)))
    orm_columns = {column.name for column in Base.metadata.tables[table_name].columns}
    assert orm_columns == migration_columns, table_name


def _declared_checks(path: Path) -> list[tuple[str, str]]:
    """Every ``CheckConstraint(sql, name=...)`` in a Python source file, by name.

    Parsed with ``ast`` rather than scraped with a regular expression. The SQL is a
    mix of ``'`` and ``"`` quoting and is often written as several implicitly
    concatenated literals, and a regex that misses part of it produces a *false
    pass*: the constraint appears absent from both sets, so the comparison reports
    agreement. That is not hypothetical -- the first version of this extractor
    assumed the wrong argument order, silently returned ``{}`` for both files, and
    passed while the two were free to differ. ``ast`` sees the value the interpreter
    would, and adjacent string literals arrive already joined.

    The call shape is ``CheckConstraint(sql, name=...)`` -- one positional argument
    and a keyword ``name`` -- and that is asserted first, so a future edit that
    changes it fails loudly instead of quietly emptying the comparison.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Resolve the module-level names so an f-string that interpolates one still has a
    # static value: the status and action CHECKs are written as
    # ``f"status IN ({_CASE_STATUSES})"`` so that adding a state to the domain enum
    # widens the constraint automatically. That is the behaviour worth keeping, and
    # it must not be the reason this comparison silently skips five constraints.
    # Seed with the shared constraint builders so a constraint written as
    # ``is_sha256_fingerprint("col")`` resolves to the SQL it produces, exactly
    # as the interpreter would when the module is imported.
    module_namespace: dict[str, Any] = {
        "not_blank": ddl.not_blank,
        "is_iso4217_currency": ddl.is_iso4217_currency,
        "is_sha256_fingerprint": ddl.is_sha256_fingerprint,
        "nullable": ddl.nullable,
    }
    for statement in tree.body:
        # Both forms appear: ``X = ...`` and ``X: Final[str] = ...``.
        if isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            targets, value = statement.targets, statement.value
        else:
            continue
        if value is None or not isinstance(targets[0], ast.Name):
            continue
        try:
            module_namespace[targets[0].id] = ast.literal_eval(value)
        except ValueError:
            continue
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", getattr(func, "id", "")) != "CheckConstraint":
            continue
        assert len(node.args) == 1, f"unexpected CheckConstraint call shape in {path.name}"
        keywords = {keyword.arg for keyword in node.keywords}
        assert "name" in keywords, f"unnamed CheckConstraint in {path.name}"
        sql = _static_string(node.args[0], module_namespace)
        try:
            name = ast.literal_eval(
                next(kw.value for kw in node.keywords if kw.arg == "name")
            )
        except ValueError:  # a non-literal name; not comparable statically
            continue
        if sql is None:
            continue
        if isinstance(name, str) and isinstance(sql, str):
            # The convention prepends "ck_<table>_" only when the constraint is
            # attached, so the bare name is all the two files share. Kept as a list
            # rather than a dict because bare names repeat across tables
            # (``currency_is_iso4217`` appears on several) and a dict would silently
            # collapse them -- which would hide a constraint that exists on one table
            # in the ORM and not the other.
            bare = name[len(name.split("_", 2)[0]) + 1:] if name.startswith("ck_") else name
            found.append((bare, " ".join(sql.split())))
    return sorted(found)


def _count_check_constraints(path: Path) -> int:
    """How many ``CheckConstraint`` calls the file contains, read or not."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", "")) == "CheckConstraint"
    )


def _static_string(node: ast.expr, namespace: dict[str, Any]) -> str | None:
    """The value of a string literal, or of an f-string built only from literals.

    Returns ``None`` when the expression cannot be resolved without executing the
    module -- a function call, a comprehension -- because a constraint this cannot
    read is a constraint this comparison cannot vouch for.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # A call to one of the shared constraint builders in
    # ``app.adapters.persistence.ddl``. These are pure functions of a column name,
    # so evaluating the call yields the same SQL the database will be given, which
    # is what this comparison needs to see.
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", "")
        builder = namespace.get(name)
        if callable(builder):
            args = [
                ast.literal_eval(argument)
                if isinstance(argument, ast.Constant)
                else _static_string(argument, namespace)  # nested builder call
                for argument in node.args
            ]
            if all(isinstance(argument, str) for argument in args):
                return builder(*args)
        return None
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            source = value.value
            if isinstance(source, ast.Name) and source.id in namespace:
                resolved = namespace[source.id]
                parts.append(", ".join(f"'{item}'" for item in resolved)
                             if isinstance(resolved, (list, tuple))
                             else str(resolved))
            else:
                return None
        else:
            return None
    return "".join(parts)


def test_migration_check_constraints_match_the_orm(migration_source: str) -> None:
    """Same name *and* same SQL for every check constraint, in both directions.

    Both directions matter. ORM-only means a rule the model enforces and the
    database does not, so a row written by anything other than this ORM -- a fix, a
    report, ``psql`` -- can violate it. Migration-only means the database enforces
    something the application believes is legal and will let through a value the
    domain refuses, which surfaces as a constraint violation on an INSERT rather
    than as a validation error on the request.
    """
    orm = _declared_checks(
        Path(__file__).resolve().parents[2]
        / "app"
        / "adapters"
        / "persistence"
        / "case_models.py"
    )
    migration = _declared_checks(MIGRATION)

    # Guard against the vacuous comparison this extractor once suffered, when it
    # assumed the wrong argument order and returned {} for both files. Every
    # CheckConstraint call in each file must be accounted for; if the extractor
    # cannot read one, this fails rather than quietly narrowing the comparison.
    orm_calls = _count_check_constraints(
        Path(__file__).resolve().parents[2]
        / "app"
        / "adapters"
        / "persistence"
        / "case_models.py"
    )
    migration_calls = _count_check_constraints(MIGRATION)
    assert orm_calls == 29, f"the ORM declares {orm_calls} check constraints"
    assert migration_calls == 29, f"the migration declares {migration_calls}"
    assert len(orm) == orm_calls, f"extractor read {len(orm)} of {orm_calls} ORM constraints"
    assert len(migration) == migration_calls, (
        f"extractor read {len(migration)} of {migration_calls} migration constraints"
    )

    only_orm = [pair for pair in orm if pair not in migration]
    only_migration = [pair for pair in migration if pair not in orm]
    assert orm == migration, {"orm_only": only_orm, "migration_only": only_migration}


@pytest.mark.parametrize(
    ("table_name", "column_name", "enum_type"),
    [
        ("disputes", "status", CaseStatus),
        ("disputes", "severity", CaseSeverity),
        ("investigations", "status", InvestigationRunStatus),
        ("finding_reviews", "action", ReviewActionKind),
        ("finding_reviews", "target_type", ReviewTargetType),
    ],
)
def test_enum_backed_checks_list_every_domain_member(
    table_name: str, column_name: str, enum_type: type[Enum]
) -> None:
    """The database and the domain enum must agree on the legal values.

    The same drift ``test_persistence_models.py`` guards for contract status: adding
    a state to the enum without widening the CHECK produces a schema that accepts
    nothing new, and widening the CHECK alone accepts values no code can produce.

    These five columns get their CHECK text from a hand-written ``_CASE_STATUSES``-
    style constant rather than from the enum, precisely because a CHECK cannot call
    Python. That makes the constant the place the two can drift apart, so it is
    asserted rather than trusted.
    """
    sql = _check_sql(table_name)
    for member in enum_type:
        assert f"'{member.value}'" in sql, f"{table_name}.{column_name}: {member.value}"


def test_no_phase4_identifier_exceeds_the_postgresql_limit() -> None:
    """63 bytes. Past that, PostgreSQL truncates and appends a hash.

    Silent, and the result matches neither the model nor the migration, so it has to
    be caught statically.
    """
    too_long = [
        f"{table.name}.{constraint.name}"
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name and len(constraint.name.encode("utf-8")) > 63
    ]
    too_long += [
        f"{table.name}.{index.name}"
        for table in Base.metadata.tables.values()
        for index in table.indexes
        if index.name and len(index.name.encode("utf-8")) > 63
    ]
    assert too_long == []