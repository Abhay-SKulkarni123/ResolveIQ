"""Recovery command for investigations a process left unfinished (Part 7).

An investigation is written ahead as ``PENDING`` and completed later (ADR-008). If
the process dies between the two writes, the marker survives as a durable record of
an unfinished run. This module is how an operator finds those markers and resolves
them, and it is deliberately a separate entry point rather than startup behaviour:
re-running a workflow is a decision, not something a restart should do silently.

Two modes, both idempotent and safe to run repeatedly:

``reap`` (default)
    Every pending run older than the retention window is moved to ``FAILED``. A run
    that old belongs to a process that is not coming back, and leaving it pending
    would pin its case in ``INVESTIGATING`` forever. The case returns to ``OPEN`` so
    it can be investigated again.

``--resume``
    Every pending run is re-run and completed in place. Use this after a restart
    when the work is still wanted; the run keeps its original identity, so the case
    does not accumulate duplicate runs.

Nothing here runs automatically. ``--dry-run`` reports what would change without
changing it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.adapters.llm import build_provider
from app.adapters.persistence.case_repository import SqlAlchemyCaseRepository
from app.adapters.persistence.database import create_engine_for_url, session_scope
from app.config import Settings, get_settings
from app.domain.cases import CaseTransitionError
from app.logging_config import configure_logging, log_event
from app.ports.cases import CaseNotFoundError
from app.services.cases import CaseService

__all__ = ["main"]

LOGGER = logging.getLogger("app.recovery")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.recovery",
        description="Resolve investigations left pending by an interrupted process.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="re-run pending investigations instead of failing them",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=None,
        help="age at which a pending run is considered abandoned (default: configured TTL)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override DATABASE_URL (default: application settings)",
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="prompt version used when resuming (default: application settings)",
    )
    return parser


def _service(settings: Settings, session: Session) -> CaseService:
    provider = build_provider(settings)
    return CaseService(
        SqlAlchemyCaseRepository(session),
        provider,
        max_attempts=settings.investigation_max_attempts,
        investigation_ttl_hours=settings.investigation_ttl_hours,
    )


def _reap(engine: Engine, settings: Settings, *, older_than: datetime, dry_run: bool) -> int:
    with session_scope(engine) as session:
        service = _service(settings, session)
        pending = service.list_recoverable_investigations(older_than=older_than)
    log_event(LOGGER, logging.INFO, "recovery.detected", count=len(pending))
    if dry_run:
        return 0
    resolved = 0
    for dispute_id, investigation_id in pending:
        try:
            with session_scope(engine) as session:
                service = _service(settings, session)
                service.fail_interrupted_investigation(dispute_id, investigation_id)
            resolved += 1
        except (CaseNotFoundError, CaseTransitionError) as exc:
            # A case that moved on since the sweep is not a failure of the command:
            # another process may have completed the run, or a reviewer decided the
            # case. Recorded and skipped, so one stale row does not stop the rest.
            log_event(
                LOGGER,
                logging.WARNING,
                "recovery.skipped",
                dispute_id=str(dispute_id),
                investigation_id=str(investigation_id),
                reason=type(exc).__name__,
            )
    log_event(LOGGER, logging.INFO, "recovery.succeeded", resolved=resolved)
    return 0


def _resume(engine: Engine, settings: Settings, *, prompt_version: str, dry_run: bool) -> int:
    now = datetime.now(timezone.utc)
    with session_scope(engine) as session:
        service = _service(settings, session)
        pending = service.list_recoverable_investigations(older_than=now)
    log_event(LOGGER, logging.INFO, "recovery.detected", count=len(pending))
    if dry_run:
        return 0
    resumed = 0
    for dispute_id, investigation_id in pending:
        try:
            with session_scope(engine) as session:
                service = _service(settings, session)
                service.resume_investigation(
                    dispute_id,
                    prompt_version=prompt_version,
                    investigation_id=investigation_id,
                )
            resumed += 1
        except (CaseNotFoundError, CaseTransitionError) as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "recovery.resume_skipped",
                dispute_id=str(dispute_id),
                investigation_id=str(investigation_id),
                error_category=type(exc).__name__,
            )
    return resumed


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine_for_url(args.database_url or settings.database_url)
    try:
        if args.resume:
            prompt_version = args.prompt_version or settings.prompt_version
            _resume(engine, settings, prompt_version=prompt_version, dry_run=args.dry_run)
        else:
            ttl_hours = (
                args.ttl_hours if args.ttl_hours is not None else settings.investigation_ttl_hours
            )
            older_than = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
            _reap(engine, settings, older_than=older_than, dry_run=args.dry_run)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    sys.exit(main())
