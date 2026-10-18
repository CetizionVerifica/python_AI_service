"""KPI-12 · custom KPI → candidate library disclosures (``POST /v1/kpi/suggest-mapping``).

ESG-lite sends the company's enabled catalogue; this module knows no
framework of its own. A disclosure is named by ``(framework, code)``, and the
title and section returned are always the catalogue's, never the model's.

1. ``shortlist``: the catalogue rows whose titles (and sections) share the
   most words with the KPI's name, question, unit and topic (deterministic),
   so the model never sees the whole catalogue.
2. The mapping model picks up to 5 from that shortlist with a ``why`` and a
   confidence. A pair not in the shortlist (so possibly not in the catalogue),
   or a repeat, is dropped.
3. Without a model, or when it fails or answers in another shape, the
   shortlist's best 5 come back with their word-match score scaled to at most
   0.5, so the FE shows them as "Check this".

Logs carry counts only, never the KPI's text.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Optional

from rapidfuzz import fuzz, utils

from app.kpi import model as kpi_model
from app.kpi.prompts import MAPPING_SYSTEM, build_mapping_prompt
from app.kpi.schemas import (
    MAX_MAPPING_SUGGESTIONS,
    WHY_MAX_CHARS,
    CatalogueDisclosure,
    MappingSuggestion,
    SuggestMappingRequest,
    SuggestMappingResponse,
)

logger = logging.getLogger(__name__)

SHORTLIST_SIZE = 60
FALLBACK_CONFIDENCE_CAP = 0.5
MAX_TOKENS = 1200

_STOP = {"and", "the", "for", "of", "to", "with", "from", "other", "all", "per", "total", "number", "how", "many", "much", "what", "was", "were", "your", "you", "did", "this", "that", "year", "during"}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _STOP]


def _match(a: str, b: str) -> bool:
    if a == b:
        return True
    n = min(len(a), len(b), 5)
    return n >= 5 and a[:n] == b[:n]


def kpi_text(request: SuggestMappingRequest) -> str:
    return " ".join(p for p in (request.name, request.question or "", request.topic or "", request.unit or "") if p).strip()


def _disclosure_text(d: CatalogueDisclosure) -> str:
    return " ".join(p for p in (d.title, d.section or "") if p)


def _score(kpi_words: list[str], text: str, d: CatalogueDisclosure) -> tuple[float, list[str]]:
    """0-100: the share of the disclosure title's words the KPI contains (90 %) plus a fuzzy tie-break (10 %); and the shared words."""
    title_words = set(_words(d.title)) or set(_words(_disclosure_text(d)))
    words = _words(_disclosure_text(d))
    shared = sorted({w for w in kpi_words if any(_match(w, x) for x in words)})
    if not kpi_words or not title_words:
        return 0.0, []
    hit = sum(1 for w in title_words if any(_match(w, x) for x in kpi_words))
    overlap = hit / len(title_words)
    return 0.9 * 100 * overlap + 0.1 * fuzz.token_set_ratio(text, _disclosure_text(d), processor=utils.default_process), shared


def shortlist(request: SuggestMappingRequest, k: int = SHORTLIST_SIZE) -> list[tuple[CatalogueDisclosure, float, list[str]]]:
    text = kpi_text(request)
    kpi_words = _words(text)
    scored = [(d, *_score(kpi_words, text, d)) for d in request.catalogue]
    scored.sort(key=lambda x: (-x[1], x[0].framework, x[0].code))
    return scored[:k]


def _confidence(raw) -> Optional[float]:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(c):
        return None
    if 1 < c <= 100:
        c = c / 100
    return round(max(0.0, min(1.0, c)), 3)


def _parse(raw: object, allowed: dict[tuple[str, str], CatalogueDisclosure]) -> tuple[list[MappingSuggestion], int]:
    items = raw.get("suggestions", []) if isinstance(raw, dict) else []
    out: list[MappingSuggestion] = []
    seen: set = set()
    dropped = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        key = (str(item.get("framework", "")).strip(), str(item.get("code", "")).strip())
        d = allowed.get(key)
        confidence = _confidence(item.get("confidence"))
        if d is None or key in seen or confidence is None:
            dropped += 1
            continue
        why = item.get("why")
        why = why.strip()[:WHY_MAX_CHARS] if isinstance(why, str) and why.strip() else f"Matches '{d.title[:80]}'"
        out.append(MappingSuggestion(framework=d.framework, code=d.code, title=d.title, section=d.section, why=why, confidence=confidence))
        seen.add(key)
        if len(out) == MAX_MAPPING_SUGGESTIONS:
            break
    return out, dropped


def _fallback(candidates: list[tuple[CatalogueDisclosure, float, list[str]]]) -> list[MappingSuggestion]:
    out = []
    for d, score, shared in candidates:
        if not shared:
            continue
        out.append(
            MappingSuggestion(
                framework=d.framework,
                code=d.code,
                title=d.title,
                section=d.section,
                why=(("Shares the word " if len(shared) == 1 else "Shares the words ") + ", ".join(f"'{w}'" for w in shared[:4]) + " with the disclosure")[:WHY_MAX_CHARS],
                confidence=round(min(FALLBACK_CONFIDENCE_CAP, score * FALLBACK_CONFIDENCE_CAP / 100), 3),
            )
        )
        if len(out) == MAX_MAPPING_SUGGESTIONS:
            break
    return out


def suggest(request: SuggestMappingRequest, ask: Optional[kpi_model.Ask] = None) -> SuggestMappingResponse:
    ask = ask if ask is not None else kpi_model.configured_ask()
    candidates = shortlist(request)
    picked: Optional[list[MappingSuggestion]] = None
    dropped = 0
    used_model = failed = False
    if ask is not None and candidates:
        used_model = True
        try:
            raw = kpi_model.parse_json(ask(MAPPING_SYSTEM, build_mapping_prompt(request, [d for d, _, _ in candidates]), MAX_TOKENS))
            if not isinstance(raw, dict) or not isinstance(raw.get("suggestions"), list):
                raise TypeError("unexpected answer shape")
            picked, dropped = _parse(raw, {(d.framework, d.code): d for d, _, _ in candidates})
        except Exception as e:  # LLMError, JSON errors, SDK errors: fall back to the word match
            failed = True
            logger.warning(f"kpi suggest-mapping model failed: {type(e).__name__}")
    suggestions = picked if picked is not None else _fallback(candidates)
    logger.info(
        f"kpi suggest-mapping: company={request.company_id} catalogue={len(request.catalogue)} shortlist={len(candidates)} "
        f"model={'failed' if failed else 'used' if used_model else 'none'} suggestions={len(suggestions)} dropped={dropped}"
    )
    return SuggestMappingResponse(suggestions=suggestions)
