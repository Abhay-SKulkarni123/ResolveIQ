"""FastAPI application factory.

Wiring only: the health probe, the middleware stack, the dispute router and the error
handlers. Everything the routes need is built in :mod:`app.api.deps`, so this module
has no knowledge of repositories, providers or sessions and can be read in one pass.

The store is chosen by configuration and never by reachability. ``create_app`` does
not touch the database, so the application starts and serves ``/api/v1/health`` even
when PostgreSQL is down; the first request that needs a case fails with a 503
``DATABASE_UNAVAILABLE`` naming the problem. Failing to start instead would turn a
database outage into an unexplained absence of the whole service, and silently
serving from memory would be worse than either.

Middleware order is stated in one place and is not incidental. Starlette builds the
stack inside-out, so the last middleware added is the outermost; the order here, outer
to inner, is CORS, request context, security headers, rate limit, trusted host. CORS is
outermost so even a throttled or rejected request carries the headers a browser needs
to read the response at all; the request context is next so everything inside it --
including the limiter's own 429 -- has a correlation id and is logged.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.errors import install_error_handlers
from app.api.hardening import RateLimitMiddleware, SecurityHeadersMiddleware
from app.api.middleware import RequestContextMiddleware
from app.api.routes import router as cases_router
from app.config import get_settings
from app.logging_config import configure_logging

__all__ = ["app", "create_app"]


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    # Tri-state ``expose_docs``: unset means "auto" (development and test serve the
    # docs, production does not). Turning the docs off removes both the Swagger UI and
    # ``/openapi.json``, which is the exhaustive map of the surface the setting exists
    # to control -- leaving the schema served but the UI hidden would defeat the point.
    serve_docs = settings.expose_docs
    if serve_docs is None:
        serve_docs = settings.app_env != "production"

    application = FastAPI(
        title="ResolveIQ API",
        version="0.4.0",
        summary="AI-assisted billing dispute investigation and resolution",
        docs_url="/docs" if serve_docs else None,
        redoc_url="/redoc" if serve_docs else None,
        openapi_url="/openapi.json" if serve_docs else None,
    )

    # Innermost first; see the module docstring for the resulting outer-to-inner order.

    # Rejects a request whose Host header is not in the allow-list, which is how a
    # cache-poisoning or password-reset host-spoofing attempt is stopped. "*" in
    # development allows everything; production refuses "*" at startup (see config).
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    # Always on in production, opt-in elsewhere so ordinary test runs are not throttled.
    if settings.rate_limit_enabled or settings.app_env == "production":
        application.add_middleware(
            RateLimitMiddleware,
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            exempt_paths=("/api/v1/health",),
        )

    if settings.security_headers_enabled:
        application.add_middleware(SecurityHeadersMiddleware)

    # Added before CORS, so CORS ends up outermost and the correlation header is on
    # every response the application itself produces, error responses included.
    application.add_middleware(RequestContextMiddleware)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        # Authorization carries the verified identity in AUTH_MODE=token; the actor
        # headers carry the demo identity in AUTH_MODE=demo. Idempotency-Key is listed
        # so the later approval endpoint does not need a CORS change to be usable.
        allow_headers=[
            "Content-Type",
            "Authorization",
            "Idempotency-Key",
            "X-Actor-Id",
            "X-Actor-Role",
        ],
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
