"""Deterministic text matching used by the rules layer: exact, normalised,
then token overlap. Nothing here knows about tenants or models."""
from __future__ import annotations

import re
from typing import Iterable, Optional

_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that carry no meaning for matching a header or an activity wording.
STOPWORDS = {"of", "the", "and", "in", "per", "for", "a", "an", "to", "total", "data", "value", "type", "name", "no"}


def normalise(text: str) -> str:
    """Lower case, punctuation out, whitespace collapsed."""
    return " ".join(_WORD_RE.findall(str(text or "").lower()))


def tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(str(text or "").lower()) if t not in STOPWORDS}


def overlap(a: str, b: str) -> float:
    """Jaccard overlap of the two texts' tokens (0..1)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def best_match(text: str, candidates: Iterable[str], min_overlap: float = 0.9) -> Optional[tuple[str, float]]:
    """``(candidate, confidence)``: 1.0 for an exact match, 0.97 for a
    normalised one, the overlap itself when ≥ ``min_overlap``; else None."""
    text_n = normalise(text)
    if not text_n:
        return None
    best: Optional[tuple[str, float]] = None
    for cand in candidates:
        if cand == text:
            return cand, 1.0
        cand_n = normalise(cand)
        if cand_n == text_n:
            score = 0.97
        else:
            score = overlap(text_n, cand_n)
            if score < min_overlap:
                continue
        if best is None or score > best[1]:
            best = (cand, score)
    return best


def contains_name(text: str, name: str) -> bool:
    """Whether a site or category name is written inside the text ("Plant 2 - Hyd" names "Plant 2")."""
    t, n = normalise(text), normalise(name)
    return bool(n) and (t == n or f" {n} " in f" {t} ")
