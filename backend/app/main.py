"""FastAPI application factory.

Deliberately minimal at the foundation stage: a health endpoint and nothing
else. The router layout is described in docs/SYSTEM_DESIGN.md §11 but is built
out in Phase 1, once the services it depends on exist.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title="ResolveIQ API",
        version="0.1.0",
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
        allow_headers=["Content-Type", "Idempotency-Key", "X-Actor-Id", "X-Actor-Role"],
    )

    @application.get("/api/v1/health", tags=["meta"])
    def health() -> dict[str, object]:
        """Liveness probe.

        Reports provider *mode* only. It never returns key material, and no
        endpoint in this service echoes configuration (NEP-009).
        """
        return {
            "status": "ok",
            "service": "resolveiq-api",
            "version": "0.1.0",
            "environment": settings.app_env,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "prompt_version": settings.prompt_version,
        }

    return application


app = create_app()
