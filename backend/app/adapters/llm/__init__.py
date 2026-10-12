"""Adapters for the LLM provider port, and the one place a provider is chosen.

``build_provider`` is shared by the API dependency and the recovery CLI so both use
the same provider for a given configuration -- a recovery that resumed a run with a
different model would record provenance that did not match the original.
"""

from __future__ import annotations

from app.adapters.llm.mock import MockLlmProvider
from app.adapters.llm.openai_compatible import OpenAiCompatibleProvider
from app.config import Settings
from app.ports.llm import LlmProvider

__all__ = ["MockLlmProvider", "OpenAiCompatibleProvider", "build_provider"]


def build_provider(settings: Settings) -> LlmProvider:
    """The configured provider.

    ``mock`` is deterministic and offline; ``openai`` is a real HTTP adapter and
    refuses to construct without a key. The choice is explicit configuration, never a
    fallback: if a deployment selects ``openai`` and the key is missing, the process
    fails rather than silently answering from the mock.
    """
    if settings.llm_provider == "mock":
        return MockLlmProvider(name=settings.llm_provider, model=settings.llm_model)
    return OpenAiCompatibleProvider(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        temperature=settings.llm_temperature,
    )
