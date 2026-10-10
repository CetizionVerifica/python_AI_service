"""Units for PCF (E2): the six units a MaterialFactor or BOM line may carry.

ESG-lite's PCF engine works in kg, t, m2, kWh, tonne.km and unit. Sheets and
ERP exports write those many ways ("kgs", "per tonne", "kgCO2e/t.km", "MWh",
"pcs"). This module reads a unit the way the invoice and bulk-upload paths do
(``excel_parser._normalise_unit`` and ``UNIT_CONVERSIONS``) and says which PCF
unit it is and by how much a quantity changes. A unit it can't place returns
``None`` with the reason, so the caller shows a warning rather than guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.services.excel_parser import UNIT_CONVERSIONS, _normalise_unit

PCF_UNITS = ("kg", "t", "m2", "kWh", "tonne.km", "unit")


@dataclass(frozen=True)
class PcfUnit:
    unit: str
    # 1 of the written unit = `per_written` of `unit`. A quantity becomes
    # quantity × per_written; a factor per written unit becomes value ÷ per_written.
    per_written: float
    reason: str


@dataclass(frozen=True)
class UnknownUnit:
    written: str
    reason: str


_SPELLINGS: dict[str, str] = {
    # mass
    "kg": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "g": "g",
    "lb": "lb",
    "t": "tonne",
    "tonne": "tonne",
    "mt": "tonne",
    "metric ton": "tonne",
    "metric tonne": "tonne",
    "metric tons": "tonne",
    # energy
    "kwh": "kwh",
    "kw h": "kwh",
    "kw-h": "kwh",
    "mwh": "mwh",
    "gj": "gj",
    "mj": "mj",
    # freight
    "tonne.km": "tonne.km",
    "tonne km": "tonne.km",
    "tonne-km": "tonne.km",
    "tonnekm": "tonne.km",
    "tonne.kilometre": "tonne.km",
    "tonne kilometre": "tonne.km",
    "t.km": "tonne.km",
    "t km": "tonne.km",
    "t-km": "tonne.km",
    "tkm": "tonne.km",
    "kg.km": "kg.km",
    "kg km": "kg.km",
    "kgkm": "kg.km",
    # area
    "m2": "m2",
    "m²": "m2",
    "sqm": "m2",
    "sq m": "m2",
    "sq. m": "m2",
    "square metre": "m2",
    "square meter": "m2",
    "square metres": "m2",
    "square meters": "m2",
    # count
    "unit": "unit",
    "units": "unit",
    "piece": "unit",
    "pieces": "unit",
    "pc": "unit",
    "pcs": "unit",
    "each": "unit",
    "ea": "unit",
    "item": "unit",
    "items": "unit",
    "nos": "unit",
    "no": "unit",
    "no.": "unit",
    "number": "unit",
}

# Canonical spelling → (PCF unit, how many PCF units one of it is).
_TO_PCF: dict[str, tuple[str, float]] = {
    "kg": ("kg", 1.0),
    "g": ("kg", 1 / UNIT_CONVERSIONS["kg"]["g"]),
    "lb": ("kg", UNIT_CONVERSIONS["lb"]["kg"]),
    "tonne": ("t", 1.0),
    "kwh": ("kWh", 1.0),
    "mwh": ("kWh", UNIT_CONVERSIONS["mwh"]["kwh"]),
    "gj": ("kWh", UNIT_CONVERSIONS["gj"]["kwh"]),
    "mj": ("kWh", UNIT_CONVERSIONS["mj"]["kwh"]),
    "tonne.km": ("tonne.km", 1.0),
    "kg.km": ("tonne.km", UNIT_CONVERSIONS["kg.km"]["tonne.km"]),
    "m2": ("m2", 1.0),
    "unit": ("unit", 1.0),
}

# Written the same for short and metric tons; never guessed.
_AMBIGUOUS = {
    "ton": "“ton” can mean a metric tonne or a short ton; write t (tonne) or convert it first",
    "tons": "“tons” can mean metric tonnes or short tons; write t (tonne) or convert it first",
}

_CO2E = re.compile(r"(k?g|t|mt|tonnes?|kilograms?|grams?)\s*(?:of\s*)?co2\s*-?\s*(?:e|eq|equivalents?)?\b", re.I)


def _text(raw) -> str:
    return re.sub(r"\s+", " ", str(raw if raw is not None else "")).replace("₂", "2").strip()


def denominator(raw) -> str:
    """'kgCO2e/kg' → 'kg', 'kg CO2e per tonne' → 'tonne', 'kg' → 'kg'."""
    text = _text(raw).replace("(", " ").replace(")", " ").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[1]
    else:
        m = re.search(r"\bper\s+(.+)$", text, re.I)
        if m:
            text = m.group(1)
        elif _CO2E.fullmatch(text.strip()):
            return ""  # only a numerator ("kgCO2e"), no unit
    return text.strip()


def to_pcf_unit(raw) -> PcfUnit | UnknownUnit:
    """The PCF unit a written unit (or 'kgCO2e/<unit>') stands for."""
    written = _text(raw)
    den = denominator(written)
    if not den:
        return UnknownUnit(written, "No unit given" if not written else f"“{written}” names no unit")
    canonical = _normalise_unit(den)
    canonical = re.sub(r"\s+", " ", canonical).strip(" .")
    if canonical in _AMBIGUOUS:
        return UnknownUnit(written, _AMBIGUOUS[canonical])
    key = _SPELLINGS.get(canonical, canonical)
    hit = _TO_PCF.get(key)
    if not hit:
        return UnknownUnit(written, f"“{written}” is not one of {', '.join(PCF_UNITS)} and can't be converted to one")
    unit, per = hit
    if per == 1.0:
        reason = f"“{written}” read as {unit}" if written != unit else f"{unit}"
    else:
        reason = f"“{written}” converted to {unit} (1 {key} = {per:g} {unit})"
    return PcfUnit(unit, per, reason)


def co2e_multiplier(raw) -> Optional[tuple[float, str]]:
    """How to turn a value written in the header's mass of CO2e into kgCO2e.

    'tCO2e/t' → (1000, …), 'gCO2e per kg' → (0.001, …), 'kgCO2e' → (1, …).
    None when the text names no CO2e mass.
    """
    text = _text(raw)
    numerator = text.split("/", 1)[0] if "/" in text else re.split(r"\bper\b", text, 1, flags=re.I)[0]
    m = _CO2E.search(numerator)
    if not m:
        return None
    mass = m.group(1).lower()
    if mass in ("kg", "kilogram", "kilograms"):
        return 1.0, "values are kgCO2e"
    if mass in ("g", "gram", "grams"):
        return 0.001, f"values in “{text}” are gCO2e, divided by 1,000 to kgCO2e"
    return 1000.0, f"values in “{text}” are tCO2e, multiplied by 1,000 to kgCO2e"
