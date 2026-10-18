"""KPI-12 · document → candidate KPI values (``POST /v1/kpi/suggest-values``).

The model reads the document's pages and points at figures for the KPIs
ESG-lite sent. Everything it answers then passes deterministic checks, and a
candidate failing any of them is dropped (never repaired by guessing):

1. ``kpi_code`` is one of the request's codes (layer rule 2).
2. ``breakdown_key`` is null or one of that KPI's breakdown keys.
3. The unit is the document's, matched by spelling only (``m³`` = ``m3``,
   ``kilolitre`` = ``kL``) to one of the KPI's allowed units (or its unit when
   no list is given). A different unit would need a conversion, so the
   candidate is dropped. Without allowed units and a unit the unit is returned
   as written.
4. The quote is on the cited page (whitespace and case aside); when it is on
   another page, ``source_page`` is corrected to that page.
5. A number must appear in its own quote (``1,200`` = 1200). This is what
   keeps a calculated total out: a sum, an average or a converted figure is
   not written in the document, so it can't be quoted (rule 1, never
   calculate).

Confidence is clamped to 0–1 (a 0–100 answer is scaled), periods that are not
ISO dates become null, duplicates go, and at most 5 candidates per KPI are
kept, best first, in the request's KPI order. Logs carry ids and counts only.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from collections import Counter
from typing import Optional, Union

from app.kpi import model as kpi_model
from app.kpi.documents import Page, StoredDocument
from app.kpi.prompts import VALUES_SYSTEM, build_values_prompt
from app.kpi.schemas import (
    MAX_CANDIDATES_PER_KPI,
    QUOTE_MAX_CHARS,
    WHY_MAX_CHARS,
    KpiAsked,
    SuggestValuesRequest,
    SuggestValuesResponse,
    ValueCandidate,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 4000


class AiUnavailable(RuntimeError):
    """No model configured, or the call failed: ESG-lite shows "AI not available right now"."""


# ---------------------------------------------------------------------------
# Units: same unit, different spelling. Never a conversion.
# ---------------------------------------------------------------------------

_UNIT_ALIASES = {
    "m3": {"m3", "cubicmetre", "cubicmetres", "cubicmeter", "cubicmeters", "cum", "cbm"},
    "kl": {"kl", "kilolitre", "kilolitres", "kiloliter", "kiloliters"},
    "l": {"l", "litre", "litres", "liter", "liters", "ltr", "ltrs", "lt"},
    "t": {"t", "tonne", "tonnes", "mt", "metrictonne", "metrictonnes", "metricton", "metrictons"},
    "kg": {"kg", "kgs", "kilogram", "kilograms", "kilogramme", "kilogrammes"},
    "kwh": {"kwh", "kilowatthour", "kilowatthours"},
    "mwh": {"mwh", "megawatthour", "megawatthours"},
    "gj": {"gj", "gigajoule", "gigajoules"},
    "%": {"%", "percent", "percentage", "pct"},
    "count": {"count", "nos", "no", "number", "numbers", "persons", "people", "employees", "headcount", "heads", "fte"},
    "hours": {"hours", "hour", "hrs", "hr", "h"},
}
_ALIAS_OF = {spelling: key for key, spellings in _UNIT_ALIASES.items() for spelling in spellings}


def unit_key(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    s = unit.strip().lower().replace("³", "3").replace("²", "2").replace("^", "")
    s = re.sub(r"[\s.()_-]", "", s)
    if not s:
        return None
    return _ALIAS_OF.get(s, s)


def match_unit(written: Optional[str], kpi: KpiAsked) -> tuple[bool, Optional[str]]:
    """(keep, unit to return)."""
    allowed = kpi.allowed_units or ([kpi.unit] if kpi.unit else [])
    written = (written or "").strip() or None
    if not allowed:
        return True, written
    if written is None:
        # Counts are often written bare ("Total employees: 245").
        counts = [u for u in allowed if unit_key(u) == "count"]
        return (True, counts[0]) if counts else (False, None)
    key = unit_key(written)
    for u in allowed:
        if unit_key(u) == key:
            return True, u
    return False, None


# ---------------------------------------------------------------------------
# Quotes and numbers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = text.replace("³", "3").replace("²", "2").replace(" ", " ").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in _NUMBER.finditer(text):
        raw = m.group(0).rstrip(",").replace(",", "")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _same_number(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def _as_value(raw) -> Optional[Union[float, str]]:
    if isinstance(raw, bool):
        return "Yes" if raw else "No"
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        plain = s.replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", plain):
            return float(plain)
        return s[:500]
    return None


def _date(raw) -> Optional[dt.date]:
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _confidence(raw) -> Optional[float]:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(c):
        return None
    if 1 < c <= 100:
        c = c / 100
    return max(0.0, min(1.0, c))


def _page_of_quote(quote: str, cited: Optional[int], pages: list[Page]) -> Optional[int]:
    q = _norm(quote)
    if not q:
        return None
    by_number = {p.number: _norm(p.text) for p in pages}
    if cited in by_number and q in by_number[cited]:
        return cited
    for number, text in by_number.items():
        if q in text:
            return number
    return None


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def filter_candidates(raw: object, request: SuggestValuesRequest, pages: list[Page]) -> tuple[list[ValueCandidate], Counter]:
    """The model's answer → the candidates that pass every check, plus a count of drops by reason."""
    kpis = {k.code: k for k in request.kpis}
    items = raw.get("candidates", []) if isinstance(raw, dict) else raw
    dropped: Counter = Counter()
    kept: dict[str, list[ValueCandidate]] = {code: [] for code in kpis}
    seen: set = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped["shape"] += 1
            continue
        code = str(item.get("kpi_code") or "").strip()
        kpi = kpis.get(code)
        if kpi is None:
            dropped["not_requested"] += 1
            continue

        breakdown = item.get("breakdown_key")
        breakdown = str(breakdown).strip() if breakdown not in (None, "") else None
        if breakdown is not None and breakdown not in kpi.breakdown_keys():
            dropped["breakdown"] += 1
            continue

        value = _as_value(item.get("value"))
        if value is None:
            dropped["no_value"] += 1
            continue

        ok, unit = match_unit(item.get("unit") if isinstance(item.get("unit"), str) else None, kpi)
        if not ok:
            dropped["unit"] += 1
            continue

        quote = item.get("quote")
        quote = quote.strip()[:QUOTE_MAX_CHARS] if isinstance(quote, str) else ""
        try:
            cited = int(item.get("source_page"))
        except (TypeError, ValueError):
            cited = None
        page = _page_of_quote(quote, cited, pages)
        if page is None:
            dropped["quote_not_found"] += 1
            continue

        if isinstance(value, float):
            if not any(_same_number(abs(value), n) for n in numbers_in(quote)):
                dropped["value_not_in_quote"] += 1
                continue
        elif _norm(value) not in _norm(quote):
            dropped["value_not_in_quote"] += 1
            continue

        confidence = _confidence(item.get("confidence"))
        if confidence is None:
            dropped["confidence"] += 1
            continue

        start, end = _date(item.get("period_start")), _date(item.get("period_end"))
        if start and end and start > end:
            start = end = None
        site = item.get("site_hint")
        site = site.strip()[:200] or None if isinstance(site, str) else None
        why = item.get("why")
        why = why.strip()[:WHY_MAX_CHARS] if isinstance(why, str) and why.strip() else f"found '{quote[:80]}' on page {page}"

        key = (code, breakdown, start, end, value, unit)
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        kept[code].append(
            ValueCandidate(
                kpi_code=code,
                value=value,
                unit=unit,
                breakdown_key=breakdown,
                period_start=start,
                period_end=end,
                site_hint=site,
                source_page=page,
                quote=quote,
                confidence=round(confidence, 3),
                why=why,
            )
        )

    out: list[ValueCandidate] = []
    for k in request.kpis:
        ranked = sorted(kept[k.code], key=lambda c: -c.confidence)
        dropped["over_cap"] += max(0, len(ranked) - MAX_CANDIDATES_PER_KPI)
        out.extend(ranked[:MAX_CANDIDATES_PER_KPI])
    if not dropped["over_cap"]:
        del dropped["over_cap"]
    return out, dropped


def suggest(
    request: SuggestValuesRequest,
    doc: StoredDocument,
    pages: list[Page],
    ask: Optional[kpi_model.Ask] = None,
) -> SuggestValuesResponse:
    ask = ask if ask is not None else kpi_model.configured_ask()
    if ask is None:
        raise AiUnavailable("no model configured")
    if not any(p.text.strip() for p in pages):
        logger.info(f"kpi suggest-values: company={request.company_id} document={doc.id} pages={len(pages)} no text")
        return SuggestValuesResponse(document_id=doc.id, document_name=doc.name, pages=len(pages), candidates=[])
    try:
        raw = kpi_model.parse_json(ask(VALUES_SYSTEM, build_values_prompt(request.kpis, request.period_hint, pages), MAX_TOKENS))
    except Exception as e:  # LLMError, JSON errors, SDK errors
        logger.warning(f"kpi suggest-values: company={request.company_id} document={doc.id} model failed: {type(e).__name__}")
        raise AiUnavailable(type(e).__name__) from e
    candidates, dropped = filter_candidates(raw, request, pages)
    logger.info(
        f"kpi suggest-values: company={request.company_id} document={doc.id} pages={len(pages)} "
        f"kpis={len(request.kpis)} candidates={len(candidates)} dropped={dict(dropped)}"
    )
    return SuggestValuesResponse(document_id=doc.id, document_name=doc.name, pages=len(pages), candidates=candidates)
