"""S3-12 · EEIO sector suggestion for a screening spend line.

``POST /v1/scope3/eeio-sector`` (ESG-lite docs/scope3/01-contracts.md §5):

    in   {lines: [{description, supplier, amount, currency}], sectors: [{code, name}]}
    out  {lines: [{suggestions: [{code, confidence 0-100}]}]}   (up to 3 per line, same order)

ESG-lite sends the sector list (its USEEIO library) and asks a person to
confirm anything below 80; this service only reads and maps (layer rule 1).

Per line:
1. ``shortlist``: the sectors whose names share the most words with the line
   (word overlap, deterministic), so the model never sees the whole library.
2. The mapping model (``OPENROUTER_MAPPING_MODEL``, the existing OpenRouter
   provider) picks up to 3 codes from that shortlist. Only the line's own
   text (description and supplier) goes in the prompt: never another line,
   never the amount, and the model is never asked for a currency.
3. Without a model, or when it fails, the shortlist's best matches are
   returned with their match score as confidence, capped below 80 so ESG-lite
   always asks for confirmation. A low model confidence is returned as is.

Logs carry counts only, never line text.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from pydantic import BaseModel, Field, field_validator
from rapidfuzz import fuzz, utils

from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_LINES = 200
MAX_SECTORS = 5000
MAX_SUGGESTIONS = 3
SHORTLIST_SIZE = 40
# Without the model a word match is only a hint: always below ESG-lite's 80 % confirmation line.
FALLBACK_CONFIDENCE_CAP = 60
_NAICS = re.compile(r"\s*\(NAICS [^)]*\)\s*$")


class EeioLine(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    supplier: Optional[str] = Field(default=None, max_length=200)
    amount: float
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class EeioSector(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=255)


class EeioSectorRequest(BaseModel):
    lines: list[EeioLine] = Field(min_length=1, max_length=MAX_LINES)
    sectors: list[EeioSector] = Field(min_length=1, max_length=MAX_SECTORS)

    @field_validator("sectors")
    @classmethod
    def unique_codes(cls, v: list[EeioSector]) -> list[EeioSector]:
        if len({s.code for s in v}) != len(v):
            raise ValueError("sector codes must be unique")
        return v


class SectorSuggestion(BaseModel):
    code: str
    confidence: int = Field(ge=0, le=100)


class LineSuggestions(BaseModel):
    suggestions: list[SectorSuggestion] = Field(max_length=MAX_SUGGESTIONS)


class EeioSectorResponse(BaseModel):
    lines: list[LineSuggestions]


# (system, user) -> the model's raw text answer
Ask = Callable[[str, str], str]


def line_text(line: EeioLine) -> str:
    """The only text of a line any matcher or model sees."""
    return " ".join(p for p in (line.description.strip(), (line.supplier or "").strip()) if p)


_STOP = {"and", "the", "for", "of", "to", "with", "from", "other", "all", "except", "pvt", "ltd", "inc", "llc", "co"}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _STOP]


def _match(a: str, b: str) -> bool:
    """Same word, or the same stem of 5+ letters (transport / transportation, manufacture / manufacturing)."""
    if a == b:
        return True
    n = min(len(a), len(b), 5)
    return n >= 5 and a[:n] == b[:n]


def _score(line_words: list[str], text: str, sector_name: str) -> float:
    """0-100: the share of the sector's words the line contains (90 %), plus a fuzzy tie-break (10 %)."""
    words = _words(sector_name)
    if not words:
        return 0.0
    hit = sum(1 for w in words if any(_match(w, x) for x in line_words))
    return 0.9 * 100 * hit / len(words) + 0.1 * fuzz.WRatio(text, sector_name, processor=utils.default_process)


def shortlist(line: EeioLine, sectors: list[EeioSector], k: int = SHORTLIST_SIZE) -> list[tuple[EeioSector, float]]:
    """The k sectors whose names best match the line's words, best first (score 0-100). Deterministic."""
    text = line_text(line)
    line_words = _words(text)
    # Words only: the "(NAICS 481000)" suffix would only add noise to the match.
    scored = [(s, _score(line_words, text, _NAICS.sub("", s.name))) for s in sectors]
    scored.sort(key=lambda x: (-x[1], x[0].code))
    return scored[:k]


SYSTEM = (
    "You map one purchase line to economic sectors of a spend-based (EEIO) emission factor list. "
    "Choose up to 3 sector codes from the list given, best first, each with a confidence from 0 to 100 "
    "that it is the sector of what was bought. Use only codes from the list. If none fits, answer with an empty list. "
    'Answer with one JSON object: {"suggestions": [{"code": "...", "confidence": 0}]}.'
)


def build_prompt(line: EeioLine, candidates: list[EeioSector]) -> str:
    """The user message: the line's description and supplier, then the candidate sectors. Nothing else."""
    parts = [f"Purchase: {line.description.strip()}"]
    if line.supplier and line.supplier.strip():
        parts.append(f"Supplier: {line.supplier.strip()}")
    parts.append("Sectors:")
    parts.extend(f"{s.code}\t{s.name}" for s in candidates)
    return "\n".join(parts)


def _openrouter_ask(system: str, user: str) -> str:
    from app.services import llm

    return llm.call_openrouter_within(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        settings.MAPPING_MODEL_TIMEOUT_S,
        model=settings.OPENROUTER_MAPPING_MODEL,
        response_format={"type": "json_object"},
        max_tokens=400,
        temperature=None,
    )


def configured_ask() -> Optional[Ask]:
    """The OpenRouter mapping model when the mapping provider is OpenRouter (or auto with its key); else None."""
    provider = (settings.MAPPING_MODEL_PROVIDER or "none").strip().lower()
    if provider == "openrouter" or (provider == "auto" and settings.OPENROUTER_API_KEY):
        return _openrouter_ask if settings.OPENROUTER_API_KEY else None
    return None


def _parse(text: str, allowed: set[str]) -> list[SectorSuggestion]:
    from app.services import llm

    data = llm._parse_json_object(text)
    items = data.get("suggestions", []) if isinstance(data, dict) else []
    out: list[SectorSuggestion] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        if code not in allowed or code in seen:
            continue
        try:
            conf = int(round(float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            continue
        out.append(SectorSuggestion(code=code, confidence=max(0, min(100, conf))))
        seen.add(code)
        if len(out) == MAX_SUGGESTIONS:
            break
    return out


def _fallback(candidates: list[tuple[EeioSector, float]]) -> list[SectorSuggestion]:
    return [
        SectorSuggestion(code=s.code, confidence=int(round(min(FALLBACK_CONFIDENCE_CAP, score * FALLBACK_CONFIDENCE_CAP / 100))))
        for s, score in candidates[:MAX_SUGGESTIONS]
        if score > 0
    ]


def suggest(request: EeioSectorRequest, ask: Optional[Ask] = None) -> EeioSectorResponse:
    """Up to 3 sectors per line, in line order."""
    ask = ask if ask is not None else configured_ask()
    model_calls = model_failures = 0
    lines: list[LineSuggestions] = []
    for line in request.lines:
        candidates = shortlist(line, request.sectors)
        picked: Optional[list[SectorSuggestion]] = None
        if ask is not None and candidates:
            model_calls += 1
            try:
                picked = _parse(ask(SYSTEM, build_prompt(line, [s for s, _ in candidates])), {s.code for s, _ in candidates})
            except Exception as e:  # LLMError, JSON errors, SDK errors: fall back to the word match
                model_failures += 1
                logger.warning(f"eeio-sector model failed: {type(e).__name__}")
        lines.append(LineSuggestions(suggestions=picked if picked is not None else _fallback(candidates)))
    logger.info(f"eeio-sector: {len(request.lines)} line(s), {len(request.sectors)} sector(s), {model_calls} model call(s), {model_failures} failed")
    return EeioSectorResponse(lines=lines)


def to_json(response: EeioSectorResponse) -> str:
    return json.dumps(response.model_dump())
