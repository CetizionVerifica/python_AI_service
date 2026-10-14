"""The ``MappingModel`` interface (XI2 2/2) and its adapters.

A model is asked once per sheet, for the parts the profile and the rules left
unresolved, and answers a ``ModelAnswer``: a strict JSON shape the engine
re-validates against the caller's context (``llm.py``). The adapters log call
counts and token usage, never prompt text or cell text.

``AnthropicMappingModel``: Claude through the Anthropic SDK with structured
output (decision 2). ``OpenRouterMappingModel``: the service's existing
OpenRouter client (``llm.py``), JSON mode. ``configured_model()`` picks one
from ``MAPPING_MODEL_PROVIDER``; with no usable key the engine is rules only.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)


class ModelError(RuntimeError):
    """The model gave no usable answer (refusal, timeout, bad JSON, outage)."""


# ---------------------------------------------------------------------------
# What the model answers (validated again against the context by llm.py)
# ---------------------------------------------------------------------------


class ModelCategoryValue(BaseModel):
    text: str
    category_id: Optional[int] = None
    confidence: float = 0.0


class ModelCategory(BaseModel):
    kind: Literal["fixed", "column"]
    category_id: Optional[int] = None
    column: Optional[str] = None
    values: list[ModelCategoryValue] = Field(default_factory=list)
    confidence: float = 0.0


class ModelMonthColumn(BaseModel):
    column: str
    date: str


class ModelPeriod(BaseModel):
    kind: Literal["column", "monthly_fixed", "yearly", "months_as_columns"]
    column: Optional[str] = None
    date: Optional[str] = None
    year_type: Optional[Literal["CY", "FY"]] = None
    year: Optional[int] = None
    columns: list[ModelMonthColumn] = Field(default_factory=list)
    confidence: float = 0.0


class ModelField(BaseModel):
    key: str
    column: Optional[str] = None
    confidence: float = 0.0


class ModelActivity(BaseModel):
    sheet_text: str
    category_id: Optional[int] = None
    factor_name: Optional[str] = None
    confidence: float = 0.0


class ModelAnswer(BaseModel):
    """One sheet's answer. Every part is optional: the model fills what it was asked for."""

    site_column: Optional[str] = None
    category: Optional[ModelCategory] = None
    period: Optional[ModelPeriod] = None
    fields: list[ModelField] = Field(default_factory=list)
    activities: list[ModelActivity] = Field(default_factory=list)
    notes: str = ""


class MappingModel(Protocol):
    name: str

    def propose(self, system: str, user: str) -> ModelAnswer:
        """One call. Raises ``ModelError`` when there is no usable answer."""
        ...


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class AnthropicMappingModel:
    """Claude through the Anthropic SDK, structured output (``messages.parse``)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, timeout: Optional[float] = None, max_retries: int = 1):
        import anthropic

        self.name = model or settings.ANTHROPIC_MAPPING_MODEL
        self._client = anthropic.Anthropic(api_key=api_key or settings.ANTHROPIC_API_KEY, timeout=timeout or settings.MAPPING_MODEL_TIMEOUT_S, max_retries=max_retries)

    def propose(self, system: str, user: str) -> ModelAnswer:
        import anthropic

        try:
            response = self._client.messages.parse(
                model=self.name,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=ModelAnswer,
            )
        except anthropic.RateLimitError as e:
            raise ModelError("The mapping model is busy; try again in a moment.") from e
        except anthropic.APIStatusError as e:
            logger.warning(f"mapping model {self.name} answered {e.status_code}")
            raise ModelError("The mapping model refused the request.") from e
        except anthropic.APIConnectionError as e:
            raise ModelError("The mapping model could not be reached.") from e
        except Exception as e:  # a truncated or unparsable answer (pydantic/JSON errors), or an SDK error not mapped above
            logger.warning(f"mapping model {self.name} failed: {type(e).__name__}")
            raise ModelError("The mapping model gave no usable answer.") from e
        usage = getattr(response, "usage", None)
        logger.info(f"mapping model {self.name} call: in={getattr(usage, 'input_tokens', '?')} out={getattr(usage, 'output_tokens', '?')} stop={response.stop_reason}")
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ModelError("The mapping model gave no answer for this sheet.")
        try:
            parsed = response.parsed_output
        except Exception as e:
            logger.warning(f"mapping model {self.name} answer did not parse: {type(e).__name__}")
            raise ModelError("The mapping model answered in an unexpected shape.") from e
        if parsed is None:
            raise ModelError("The mapping model gave no answer for this sheet.")
        return parsed


class OpenRouterMappingModel:
    """The existing OpenRouter client (OCR extraction) in JSON mode."""

    def __init__(self, model: Optional[str] = None, budget_s: Optional[float] = None):
        self.name = model or settings.OPENROUTER_MODEL
        self._budget = budget_s or settings.MAPPING_MODEL_TIMEOUT_S

    def propose(self, system: str, user: str) -> ModelAnswer:
        from app.services import llm

        schema = ModelAnswer.model_json_schema()
        messages = [{"role": "system", "content": system + "\n\nAnswer with one JSON object matching this schema:\n" + _json(schema)}, {"role": "user", "content": user}]
        try:
            text = llm.call_openrouter_within(messages, self._budget)
            data = llm._parse_json_object(text)
        except Exception as e:  # LLMError, JSON errors, SDK errors
            logger.warning(f"mapping model {self.name} failed: {type(e).__name__}")
            raise ModelError("The mapping model gave no usable answer.") from e
        logger.info(f"mapping model {self.name} call: answer={len(text)} chars")
        try:
            return ModelAnswer.model_validate(data)
        except ValidationError as e:
            raise ModelError("The mapping model answered in an unexpected shape.") from e


def _json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


_warned = False
_cached: tuple[tuple, MappingModel] | None = None


def configured_model() -> Optional[MappingModel]:
    """The adapter ``MAPPING_MODEL_PROVIDER`` names, or None (rules only).
    One adapter (and one HTTP client) per process, rebuilt when the settings change."""
    global _warned, _cached
    provider = (settings.MAPPING_MODEL_PROVIDER or "none").strip().lower()
    if provider == "anthropic":
        if not settings.ANTHROPIC_API_KEY:
            if not _warned:
                logger.warning("MAPPING_MODEL_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty: smart import runs on rules only")
                _warned = True
            return None
        key = ("anthropic", settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MAPPING_MODEL, settings.MAPPING_MODEL_TIMEOUT_S)
        factory = AnthropicMappingModel
    elif provider == "openrouter":
        key = ("openrouter", settings.OPENROUTER_MODEL, settings.MAPPING_MODEL_TIMEOUT_S)
        factory = OpenRouterMappingModel
    else:
        return None
    if _cached is None or _cached[0] != key:
        _cached = (key, factory())
    return _cached[1]
