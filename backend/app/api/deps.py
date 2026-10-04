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

**Identity is a header and is labelled as such.** ``X-Actor-Id`` and ``X-Actor-Role``
are read from the request and stored as a label on a review annotation. They are not
authentication (OQ-01): trivially spoofable, no signature, no session. The role is
recorded so a reviewer can see who claimed what, and it is **not** consulted for any
authorisation decision -- separation of duties is a later phase, and a role check
built on a spoofable header would appear to work and would not.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.adapters.llm.mock import MockLlmProvider
from app.adapters.persistence.case_repository import SqlAlchemyCaseRepository
from app.adapters.persistence.database import get_sessionmaker
from app.adapters.persistence.memory_case_repository import InMemoryCaseRepository
from app.config import Settings, get_settings
from app.ports.llm import LlmProvider
from app.services.cases import CaseService

__all__ = ["Actor", "CaseServiceDep", "SettingsDep", "StoreKind", "actor", "settings", "store_kind"]


@dataclass(frozen=True, slots=True)
class Actor:
    """Who the request claims to be.

    A record of a claim, nothing more. ``role`` is carried so it can be displayed and
    stored; no route checks it, and a future authorisation layer must not inherit it
    from here by accident.
    """

    id: str
    role: str


#: ``"postgres"`` or ``"memory"``. Reported by ``GET /api/v1/capabilities`` so the
#: reviewer UI can say which store answered rather than leaving it to be inferred.
StoreKind = str


def settings() -> Settings:
    return get_settings()


def store_kind(settings_dep: Annotated[Settings, Depends(settings)]) -> StoreKind:
    return settings_dep.case_store


def actor(
    settings_dep: Annotated[Settings, Depends(settings)],
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
) -> Actor:
    """Read the development identity from request headers.

    Falls back to the configured default so the API is usable from a browser without
    a header, and so a reviewer using the UI is never blocked by a missing one. That
    convenience is precisely why this is not authentication: it grants an identity to
    anyone who asks.
    """
    if not settings_dep.demo_identity_enabled:
        raise _identity_disabled()
    actor_id = (x_actor_id or settings_dep.demo_default_actor_id).strip()
    role = (x_actor_role or settings_dep.demo_default_actor_role).strip()
    if not actor_id:
        raise _identity_invalid("X-Actor-Id must not be blank")
    if not role:
        raise _identity_invalid("X-Actor-Role must not be blank")
    return Actor(id=actor_id[:128], role=role[:64])


def _identity_disabled() -> Exception:
    from app.api.errors import ApiError

    return ApiError(
        401,
        "IDENTITY_REQUIRED",
        "Demo identity is disabled and this build has no authentication. "
        "Send X-Actor-Id and X-Actor-Role.",
    )


def _identity_invalid(message: str) -> Exception:
    from app.api.errors import ApiError

    return ApiError(422, "INVALID_IDENTITY", message)


def build_provider(settings_dep: Settings) -> LlmProvider:
    """The configured model provider.

    ``llm_provider`` is a ``Literal["mock"]``, so this cannot accidentally build a
    networked client. Adding a real provider is a config change *and* a code change,
    which is deliberate: a phase that adds a hosted model must also add the budget cap,
    the redaction and the prompt-injection framing that go with it (NEP-004, NEP-005).
    """
    return MockLlmProvider(name=settings_dep.llm_provider, model=settings_dep.llm_model)


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
    if settings_dep.case_store == "memory":
        yield CaseService(memory_repository(request), provider), "memory"
        return

    session: Session = get_sessionmaker()()
    try:
        yield CaseService(SqlAlchemyCaseRepository(session), provider), "postgres"
        # Commit only after the route returned cleanly. A route that raises after a
        # successful ``save`` leaves nothing behind, so a caller that retries cannot
        # find a half-written case.
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(settings)]
ActorDep = Annotated[Actor, Depends(actor)]
CaseServiceDep = Annotated[tuple[CaseService, StoreKind], Depends(case_service)]