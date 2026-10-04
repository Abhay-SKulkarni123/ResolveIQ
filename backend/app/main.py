"""FastAPI application factory.

Wiring only: the health probe, the CORS policy, the dispute router and the error
handlers. Everything the routes need is built in :mod:`app.api.deps`, so this module
has no knowledge of repositories, providers or sessions and can be read in one pass.

The store is chosen by configuration and never by reachability. ``create_app`` does
not touch the database, so the application starts and serves ``/api/v1/health`` even
when PostgreSQL is down; the first request that needs a case fails with a 503
``DATABASE_UNAVAILABLE`` naming the problem. Failing to start instead would turn a
database outage into an unexplained absence of the whole service, and silently
serving from memory would be worse than either.

The one thing the factory does insist on is that ``case_store`` be spelled correctly.
An unrecognised value raises at construction: a typo in a deployment variable should
fail loudly and immediately, not select a store by accident.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes import router as cases_router
from app.config import get_settings

__all__ = ["app", "create_app"]


def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title="ResolveIQ API",
        version="0.4.0",
        summary="AI-assisted billing dispute investigation and resolution",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        # The frontend sends X-Actor-Id / X-Actor-Role in demo mode (OQ-01).
        # Idempotency-Key is listed for the later approval endpoint; nothing reads it
        # yet, and a header nobody sends cannot be relied on.
        allow_headers=["Content-Type", "Idempotency-Key", "X-Actor-Id", "X-Actor-Role"],
    )

    install_error_handlers(application)
    application.include_router(cases_router)

    @application.get("/api/v1/health", tags=["meta"])
    def health() -> dict[str, object]:
        """Liveness probe.

        Reports provider *mode* only. It never returns key material, and no endpoint
        in this service echoes configuration (NEP-009).

        Deliberately does not check the database. This endpoint answers "is the
        process up", and a readiness probe that also touches PostgreSQL turns every
        database blip into a restart of every replica. Reachability is reported per
        request, as a 503 on the call that needed it.
        """
        return {
            "status": "ok",
            "service": "resolveiq-api",
            "version": "0.4.0",
            "environment": settings.app_env,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "prompt_version": settings.prompt_version,
        }

    return application


app = create_app()
