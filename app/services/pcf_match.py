"""Material → factor matching (E2 2/3): which MaterialFactor fits a BOM line.

The caller sends the lines and the factors it may use (C04's list, scoped by
ESG-lite to the company), so nothing here reads the database or crosses a
company. Each line gets up to ``TOP`` suggestions ranked by name similarity
(rapidfuzz) with boosts for the group the name implies, a matching unit, the
geography hint and the recycled variant the line asks for. Every suggestion
carries a ``confidence`` (0–100, never above 90) and a ``reason`` the UI can
show, e.g. “EC-grade aluminium ingot, GCC → Primary aluminium, Middle East”.
When the best name match is weak and the LLM is available, it picks among the
top candidates; its pick is marked ``by: "ai"`` and capped at 85.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

from rapidfuzz import fuzz, process

from app.services.pcf_sheet import infer_group
from app.services.pcf_units import PcfUnit, to_pcf_unit

logger = logging.getLogger(__name__)

TOP = 3
AI_BELOW = 60
AI_CANDIDATES = 8
# Factors fully scored per line: the closest names (two similarity measures)
# plus every factor of the group the line's name implies. The rest of the
# library can't score above a weak group-less match and is skipped, so a
# request of many lines against a large library stays linear in both.
NAME_CANDIDATES = 40
NAME_CUTOFF = 20
RECYCLED_WORDS = ("recycled", "secondary", "scrap", "post-consumer", "post consumer", "regrind")
GEO_ALIASES: dict[str, tuple[str, ...]] = {
    "bahrain": ("bh", "bahrain", "gcc", "middle east", "me", "gulf"),
    "gcc": ("gcc", "gulf", "middle east", "me", "bahrain", "saudi", "uae", "qatar", "oman", "kuwait"),
    "middle east": ("me", "middle east", "gcc", "gulf"),
    "europe": ("eu", "europe", "european", "rer", "de", "fr", "it", "es", "nl", "uk", "gb"),
    "india": ("in", "india", "indian"),
    "china": ("cn", "china", "chinese"),
    "global": ("glo", "global", "world", "row"),
}

AI_PICK_PROMPT = """\
You match a material from a product's bill of materials to the closest emission-factor dataset.
Return ONLY a JSON object: {"material_factor_id": <id or null>, "confidence": <0-100>, "reason": "<one short sentence>"}
Pick only from the candidates given; return null when none fits the material. Prefer the same material, then the same region, then the same unit.
"""


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()


def _geo_match(hint: Optional[str], geography: Optional[str]) -> bool:
    if not hint or not geography:
        return False
    h, g = _norm(hint), _norm(geography)
    if h == g or h in g or g in h:
        return True
    for key, aliases in GEO_ALIASES.items():
        if (h == key or h in aliases) and (g == key or g in aliases):
            return True
    return False


def _wants_recycled(line: dict[str, Any]) -> bool:
    name = _norm(line.get("name"))
    share = line.get("recycled_share_pct")
    try:
        share = float(share) if share is not None else 0.0
    except (TypeError, ValueError):
        share = 0.0
    return share >= 50 or any(w in name for w in RECYCLED_WORDS)


class _LineCtx:
    """What a line contributes to every score, worked out once per line, not per factor."""

    __slots__ = ("line", "name", "group", "unit", "hint", "recycled")

    def __init__(self, line: dict[str, Any], geography_hint: Optional[str]):
        self.line = line
        self.name = _norm(line.get("name"))
        self.group = line.get("material_group") or (infer_group(str(line.get("name") or "")) or (None,))[0]
        self.unit = to_pcf_unit(line["unit"]) if line.get("unit") else None
        self.hint = geography_hint or line.get("origin_country")
        self.recycled = _wants_recycled(line)


def score(line: dict[str, Any], factor: dict[str, Any], geography_hint: Optional[str]) -> tuple[int, str]:
    """(confidence 0–90, reason) for one line against one factor."""
    return _score(_LineCtx(line, geography_hint), factor, _norm(factor.get("name")))


def _score(ctx: _LineCtx, factor: dict[str, Any], fname: str) -> tuple[int, str]:
    line = ctx.line
    sim = max(fuzz.token_set_ratio(ctx.name, fname), fuzz.partial_ratio(ctx.name, fname) * 0.9)
    conf = sim * 0.7  # a perfect name alone is 70
    why: list[str] = [f"“{line.get('name')}” → {factor.get('name')}"]

    line_group = ctx.group
    if line_group and factor.get("material_group"):
        if line_group == factor["material_group"]:
            conf += 12
            why.append(f"same group ({line_group})")
        else:
            conf -= 25
            why.append(f"group differs ({line_group} vs {factor['material_group']})")

    unit = line.get("unit")
    if unit and factor.get("unit"):
        u = ctx.unit
        if isinstance(u, PcfUnit) and u.unit == factor["unit"]:
            conf += 8
        elif isinstance(u, PcfUnit) and {u.unit, factor["unit"]} <= {"kg", "t"}:
            conf += 6  # mass either way; the engine converts
        else:
            conf -= 15
            why.append(f"unit {factor['unit']} differs from {unit}")

    hint = ctx.hint
    if hint and factor.get("geography"):
        if _geo_match(hint, factor["geography"]):
            conf += 10
            why.append(f"{factor['geography']} covers {hint}")
        else:
            conf -= 4
            why.append(f"{factor['geography']} dataset")
    elif factor.get("geography"):
        why.append(f"{factor['geography']} dataset")

    recycled = bool(factor.get("recycled_variant"))
    if recycled != ctx.recycled:
        conf -= 20 if recycled else 5
        why.append("recycled variant" if recycled else "primary variant")

    if factor.get("source"):
        why.append(f"{factor['source']}" + (f" {factor['source_year']}" if factor.get("source_year") else ""))
    return int(max(0, min(90, round(conf)))), ", ".join(why)


def _ai_pick(line: dict[str, Any], candidates: list[dict[str, Any]], call_llm: Callable[[list[dict]], str]) -> Optional[dict[str, Any]]:
    from app.services.llm import _parse_json_object

    ids = {c["material_factor_id"] for c in candidates}
    payload = {
        "material": {k: line.get(k) for k in ("name", "unit", "stage", "origin_country", "recycled_share_pct", "supplier_name")},
        "candidates": [
            {k: c.get(k) for k in ("material_factor_id", "name", "material_group", "unit", "geography", "source", "source_year", "recycled_variant")}
            for c in candidates
        ],
    }
    data = _parse_json_object(call_llm([{"role": "system", "content": AI_PICK_PROMPT}, {"role": "user", "content": json.dumps(payload, default=str)[:8000]}]))
    if not isinstance(data, dict) or data.get("material_factor_id") not in ids:
        return None
    try:
        conf = int(max(0, min(85, float(data.get("confidence", 50)))))
    except (TypeError, ValueError):
        conf = 50
    return {
        "material_factor_id": data["material_factor_id"],
        "confidence": conf,
        "reason": str(data.get("reason") or "Picked by the AI matcher").strip()[:200],
        "by": "ai",
    }


def match_factors(
    lines: list[dict[str, Any]],
    factors: list[dict[str, Any]],
    geography_hint: Optional[str] = None,
    call_llm: Optional[Callable[[list[dict]], str]] = None,
    top: int = TOP,
) -> dict[str, Any]:
    results = []
    ai_used = False
    fnames = [_norm(f.get("name")) for f in factors]
    by_group: dict[str, list[int]] = {}
    for i, f in enumerate(factors):
        if f.get("material_group"):
            by_group.setdefault(str(f["material_group"]), []).append(i)
    for line in lines:
        ctx = _LineCtx(line, geography_hint)
        picked: set[int] = set(by_group.get(str(ctx.group), ())) if ctx.group else set()
        if fnames:
            for scorer in (fuzz.token_set_ratio, fuzz.partial_ratio):
                picked.update(i for _, _, i in process.extract(ctx.name, fnames, scorer=scorer, limit=NAME_CANDIDATES, score_cutoff=NAME_CUTOFF))
        scored = sorted(((*_score(ctx, factors[i], fnames[i]), factors[i]) for i in sorted(picked)), key=lambda t: -t[0])
        suggestions = [
            {"material_factor_id": f["material_factor_id"], "name": f.get("name"), "confidence": c, "reason": r, "by": "rule"}
            for c, r, f in scored[:top]
            if c > 0
        ]
        best = suggestions[0]["confidence"] if suggestions else 0
        if best < AI_BELOW and call_llm is not None and scored:
            try:
                pick = _ai_pick(line, [f for _, _, f in scored[:AI_CANDIDATES]], call_llm)
            except Exception:
                logger.warning("AI factor pick failed; keeping the rule ranking", exc_info=True)
                pick = None
            if pick:
                ai_used = True
                pick["name"] = next((f.get("name") for f in factors if f["material_factor_id"] == pick["material_factor_id"]), None)
                suggestions = [pick] + [s for s in suggestions if s["material_factor_id"] != pick["material_factor_id"]][: top - 1]
        results.append(
            {
                "id": line.get("id"),
                "name": line.get("name"),
                "suggestions": suggestions,
                "unmatched": not suggestions or suggestions[0]["confidence"] < AI_BELOW,
            }
        )
    return {
        "lines": results,
        "total_lines": len(results),
        "unmatched_lines": sum(1 for r in results if r["unmatched"]),
        "ai_used": ai_used,
    }
