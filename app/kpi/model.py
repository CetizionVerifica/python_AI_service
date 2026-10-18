"""KPI-12 · the model both suggestions use: the existing OpenRouter mapping
provider (``OPENROUTER_MAPPING_MODEL`` through ``llm.call_openrouter_within``),
as S3-12's EEIO suggestion does. No new provider and no new key.

``Ask`` is ``(system, user, max_tokens) -> raw text``; tests pass a fake.
"""
from __future__ import annotations

from typing import Callable, Optional

from app.core.config import settings

Ask = Callable[[str, str, int], str]


def _openrouter_ask(system: str, user: str, max_tokens: int) -> str:
    from app.services import llm

    return llm.call_openrouter_within(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        settings.MAPPING_MODEL_TIMEOUT_S,
        model=settings.OPENROUTER_MAPPING_MODEL,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=None,
    )


def configured_ask() -> Optional[Ask]:
    """The OpenRouter mapping model when the mapping provider is OpenRouter (or auto with its key); else None."""
    provider = (settings.MAPPING_MODEL_PROVIDER or "none").strip().lower()
    if provider == "openrouter" or (provider == "auto" and settings.OPENROUTER_API_KEY):
        return _openrouter_ask if settings.OPENROUTER_API_KEY else None
    return None


def parse_json(text: str):
    from app.services import llm

    return llm._parse_json_object(text)
