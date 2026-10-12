"""Dependency wiring: which repository, which provider, whose identity.

Everything the routes need is built here and nowhere else, so a test can replace one
piece without a route knowing there is a choice to make.

Two decisions deserve their reasoning.

**The store is chosen explicitly, never as a fallback.** ``case_store`` is read from
settings and defaults to ``postgres``. If PostgreSQL is unreachable the application
refuses to build the dependency and says so -- it does not quietly serve from memory.
A service that degraded to an in-process store on a connection error would lose every
case on restart while answering 200 to every request, which is the specific failure
the explicit setting exists to prevent. ``memory`` is a deliberate choice for
development and tests, and :func:`store_kind` reports which one is live so a caller
never has to guess.

**One transaction per request.** The dependency opens the session, the route calls
``save``, and the dependency commits on a clean exit or rolls back on an exception.
Committing inside the route instead would leave a route that raised after saving --
and several do raise after saving, when a response cannot be built -- with rows
already durable and no record of it. Holding the transaction to the edge of the
request also gives §10's "no partial writes" for free: a case and its investigations
become durable together or not at all.

**Identity comes from an identity provider, not from the route.** Which provider is
built is configuration: ``AUTH_MODE=demo`` keeps the OQ-01 ``X-Actor-Id`` /
``X-Actor-Role`` headers (a label, ``authenticated=False``), and ``AUTH_MODE=token``
verifies a signed bearer token and returns ``authenticated=True``. Every route depends
on one :class:`Actor` either way, so nothing downstream changes when the mechanism
does. Production refuses to start on the demo mode, so the spoofable-header path cannot
be the one a deployment ships. The role is consulted in exactly one place -- the
decision endpoint insists on ``reviewer`` (:func:`app.domain.identity.can_decide`),
because a closing verdict permanently changes the lifecycle -- and it is the *verified*
role whenever a token was presented.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.adapters.auth import DemoIdentityProvider, HmacTokenIdentityProvider
from app.adapters.llm import build_provider
from app.adapters.persistence.case_repository import SqlAlchemyCaseRepository
from app.adapters.persistence.database import get_sessionmaker
from app.adapters.persistence.memory_case_repository import InMemoryCaseRepository
from app.config import Settings, get_settings
from app.logging_config import error_category, log_event
from app.ports.auth import AuthenticationError, Credentials, IdentityProvider, Principal
from app.ports.cases import CaseRepository
from app.services.cases import CaseService

__all__ = [
    "Actor",
    "CaseServiceDep",
    "IdempotencyKeyDep",
    "SettingsDep",
    "StoreKind",
    "actor",
    "idempotency_key",
    "settings",
    "store_kind",
]

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Actor:
    """The identity that resolved this request, and whether it was verified.

    ``authenticated`` is ``False`` for the demo header path and ``True`` when a provider
    verified a credential; ``source`` names the mechanism. Both are carried into
    ``/capabilities`` and the audit trail so a consumer can never read a declared
    identity as a verified one. ``id``/``role`` keep the names the routes already use.
    """

    id: str
    role: str
    authenticated: bool = False
    source: str = "demo-header"

    @classmethod
    def from_principal(cls, principal: Principal) -> Actor:
        return cls(
            id=principal.subject,
            role=principal.role,
            authenticated=principal.authenticated,
            source=principal.source,
        )


#: ``"postgres"`` or ``"memory"``. Reported by ``GET /api/v1/capabilities`` so the
#: reviewer UI can say which store answered rather than leaving it to be inferred.
StoreKind = str


def settings() -> Settings:
    return get_settings()


def store_kind(settings_dep: Annotated[Settings, Depends(settings)]) -> StoreKind:
    return settings_dep.case_store


def identity_provider(
    settings_dep: Annotated[Settings, Depends(settings)],
) -> IdentityProvider:
    """Build the configured identity provider.

    ``token`` verifies a signed bearer token and refuses to construct with a weak or
    missing secret; ``demo`` keeps the header path. The choice is configuration only --
    routes depend on the resolved :class:`Actor`, not on the mechanism.
    """
    if settings_dep.auth_mode == "token":
        return HmacTokenIdentityProvider(
            secret=settings_dep.auth_secret,
            issuer=settings_dep.auth_issuer,
            leeway_seconds=settings_dep.auth_leeway_seconds,
        )
    return DemoIdentityProvider(
        enabled=settings_dep.demo_identity_enabled,
        default_actor_id=settings_dep.demo_default_actor_id,
        default_actor_role=settings_dep.demo_default_actor_role,
    )


def credentials(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
) -> Credentials:
    """The raw identity material on the request: the bearer token and the demo headers.

    Read once here so the provider receives a plain value and the route layer never
    touches a header.
    """
    return Credentials(
        authorization=authorization,
        actor_id=x_actor_id,
        actor_role=x_actor_role,
    )


def actor(
    provider: Annotated[IdentityProvider, Depends(identity_provider)],
    creds: Annotated[Credentials, Depends(credentials)],
) -> Actor:
    """Resolve the request identity, or fail with the provider's error.

    An :class:`AuthenticationError` carries the stable code and HTTP status the envelope
    should use, so this maps it to an ``ApiError`` rather than inventing one here. The
    provider's message is safe to surface (it never names the credential), but the
    underlying reason is not logged with it.
    """
    try:
        principal = provider.authenticate(creds)
    except AuthenticationError as exc:
        raise _authentication_error(exc) from exc
    return Actor.from_principal(principal)


def _authentication_error(exc: AuthenticationError) -> Exception:
    from app.api.errors import ApiError

    return ApiError(exc.status_code, exc.code, str(exc))


def idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> str | None:
    """The client's idempotency token for this request, or ``None``.

    A header rather than a body field because it names the *request*, not the
    resource: two logically identical bodies with different keys are two operations,
    and the same key with a changed body is a client error the store will not catch.
    Blank and whitespace-only values collapse to ``None`` so they cannot be stored as
    a key that no retry could ever match.
    """
    if value is None:
        return None
    key = value.strip()
    return key or None


def memory_repository(request: Request) -> InMemoryCaseRepository:
    """One in-process store per application, not per request.

    Per request would make every case vanish between two calls: a POST returning 201
    followed by a GET 404, which looks like a broken API and is exactly what a
    reviewer would hit. Keyed on the app so it lives as long as the process, and its
    lifetime is still the process -- nothing is persisted, which is why it must never
    be the default.
    """
    existing = getattr(request.app.state, "memory_case_repository", None)
    if existing is None:
        existing = InMemoryCaseRepository()
        request.app.state.memory_case_repository = existing
    return existing


def case_service(
    request: Request,
    settings_dep: Annotated[Settings, Depends(settings)],
) -> Iterator[tuple[CaseService, StoreKind]]:
    """Yield the case service and which store backs it, committing at the edge.

    For the memory store there is nothing to commit; the dependency still yields, so
    routes have one signature rather than two.
    """
    provider = build_provider(settings_dep)

    def build_service(repository: CaseRepository) -> CaseService:
        return CaseService(
            repository,
            provider,
            max_attempts=settings_dep.investigation_max_attempts,
            investigation_ttl_hours=settings_dep.investigation_ttl_hours,
        )

    if settings_dep.case_store == "memory":
        yield build_service(memory_repository(request)), "memory"
        return

    session: Session = get_sessionmaker()()
    try:
        yield build_service(SqlAlchemyCaseRepository(session)), "postgres"
        # Commit only after the route returned cleanly. A route that raises after a
        # successful ``save`` leaves nothing behind, so a caller that retries cannot
        # find a half-written case.
        session.commit()
    except Exception as exc:
        # Recorded at ``WARNING`` because a rolled-back transaction is a request that
        # did not do what it asked and a client is about to get a 5xx. The exception
        # message is not logged, only its category: it can carry request data.
        session.rollback()
        log_event(
            LOGGER,
            logging.WARNING,
            "db.transaction.rolledback",
            store="postgres",
            error_category=error_category(exc),
        )
        raise
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(settings)]
ActorDep = Annotated[Actor, Depends(actor)]
CaseServiceDep = Annotated[tuple[CaseService, StoreKind], Depends(case_service)]
IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_key)]