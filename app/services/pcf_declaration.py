"""Supplier declaration reading (E2 phase 3, for C06).

A supplier answers a data request with a PCF/EPD PDF or a PACT JSON. This
module turns either into the fields a Manager reviews before accepting the
figure as a company MaterialFactor: declared unit, kgCO2e per declared unit,
boundary, standard, GWP set, reference period, validity and verifier.

Every field carries a ``confidence`` (0–100) and a ``reason`` the UI can show.
A PACT JSON is read deterministically (``by: "pact"``); a PDF goes through OCR
and the AI (``by: "ai"``, never above 85). A boundary other than the caller's
is flagged in ``boundary_mismatch`` and a warning, never accepted silently.
Nothing here saves to ESG-lite.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional

from app.services.pcf_units import PCF_UNITS, PcfUnit, co2e_multiplier, to_pcf_unit

logger = logging.getLogger(__name__)

PDF_TEXT_LIMIT = 24_000
AI_MAX_CONFIDENCE = 85
LOW_CONFIDENCE = 60

BOUNDARIES = ("cradle_to_gate", "cradle_to_grave", "gate_to_gate")
FIELDS = (
    "product_name",
    "supplier_name",
    "declared_unit",
    "value_kgco2e",
    "boundary",
    "standard",
    "gwp_set",
    "reference_start",
    "reference_end",
    "valid_from",
    "valid_to",
    "verifier",
)

# PACT declared units → the engine's units (and how many engine units one is).
_PACT_UNITS: dict[str, tuple[str, float]] = {
    "kilogram": ("kg", 1.0),
    "kilowatt hour": ("kWh", 1.0),
    "megajoule": ("kWh", 1 / 3.6),
    "square meter": ("m2", 1.0),
    "ton kilometer": ("tonne.km", 1.0),
    "piece": ("unit", 1.0),
}

_STANDARDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"iso\s*(?:standard\s*)?-?\s*14067", re.I), "iso14067"),
    (re.compile(r"ghg\s*protocol|product\s+life\s*cycle\s+accounting", re.I), "ghg_protocol_product"),
    (re.compile(r"pas\s*-?\s*2050", re.I), "pas2050"),
    (re.compile(r"en\s*-?\s*15804", re.I), "en15804"),
    (re.compile(r"iso\s*(?:standard\s*)?-?\s*1404[04]", re.I), "iso14044"),
    (re.compile(r"iso\s*(?:standard\s*)?-?\s*14025", re.I), "iso14025"),
]


def _add_years(d: date, years: int) -> date:
    """The same day `years` later; 29 Feb lands on 28 Feb when the target year has none."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


class DeclarationError(Exception):
    """The file can't be read as a declaration; the message is for the person."""


@dataclass
class Field:
    value: Any
    confidence: int
    reason: str
    by: str  # "pact" | "ai" | "rule"


def _empty(reason: str = "Not found in the document") -> Field:
    return Field(None, 0, reason, "rule")


def _text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def _number(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    s = _text(v).replace("−", "-")
    s = re.sub(r"[^\d.,\-eE]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if s.count(",") == 1 and len(s.split(",")[1]) != 3 else s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def to_iso_date(v: Any) -> Optional[str]:
    """'2024-01-01T00:00:00Z', '01/03/2024' (day first), '1.3.2024', 'March 2024' → ISO date; None when unreadable."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = _text(v)
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(?:(\d{1,2})\s+)?([A-Za-z]{3})[A-Za-z]*\.?,?\s+(\d{4})$", s)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1) or 1)).isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{4})$", s)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def boundary_from(text: Any) -> Optional[str]:
    t = _text(text).lower().replace("–", "-")
    if not t:
        return None
    if re.search(r"cradle\s*-?\s*to\s*-?\s*grave|a1\s*-\s*[cd]\d|full\s+life\s*-?\s*cycle", t):
        return "cradle_to_grave"
    if re.search(r"cradle\s*-?\s*to\s*-?\s*gate|a1\s*-\s*a3|upstream\s+and\s+production", t):
        return "cradle_to_gate"
    if re.search(r"gate\s*-?\s*to\s*-?\s*gate", t):
        return "gate_to_gate"
    if t in BOUNDARIES:
        return t
    return None


def standard_from(text: Any) -> Optional[str]:
    t = _text(text)
    for pattern, code in _STANDARDS:
        if pattern.search(t):
            return code
    return None


def gwp_from(text: Any) -> Optional[str]:
    t = _text(text).upper()
    m = re.search(r"\bAR\s*(\d)\b", t)
    if m:
        return f"AR{m.group(1)}"
    return None


# ----------------------------------------------------------------- PACT ---


def _is_pact(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("pcf"), dict) and ("specVersion" in data or "declaredUnit" in data["pcf"] or "declaredUnitOfMeasurement" in data["pcf"])


def _read_pact(data: dict[str, Any]) -> tuple[dict[str, Field], list[str], list[str]]:
    pcf: dict[str, Any] = data["pcf"]
    spec = _text(data.get("specVersion")) or "unknown"
    src = f"PACT {spec} field"
    warnings: list[str] = []
    problems: list[str] = []
    f: dict[str, Field] = {k: _empty() for k in FIELDS}

    product = _text(data.get("productNameCompany")) or _text(data.get("productDescription"))
    if product:
        f["product_name"] = Field(product, 100, f"{src} productNameCompany", "pact")
    company = _text(data.get("companyName"))
    if company:
        f["supplier_name"] = Field(company, 100, f"{src} companyName", "pact")

    written_unit = _text(pcf.get("declaredUnitOfMeasurement") or pcf.get("declaredUnit")).lower()
    amount = _number(pcf.get("declaredUnitAmount") if "declaredUnitAmount" in pcf else pcf.get("unitaryProductAmount"))
    hit = _PACT_UNITS.get(written_unit)
    per_engine = 1.0
    if hit:
        unit, per_engine = hit
        f["declared_unit"] = Field(unit, 100, f"{src} declaredUnit “{written_unit}”" + (f", 1 {written_unit} = {per_engine:g} {unit}" if per_engine != 1 else ""), "pact")
    elif written_unit:
        f["declared_unit"] = Field(written_unit, 20, f"PACT declared unit “{written_unit}” has no PCF unit (kg, t, m2, kWh, tonne.km, unit)", "pact")
        problems.append(f"Declared unit “{written_unit}” can't be converted to a PCF unit")
    else:
        problems.append("No declared unit in the PACT file")

    raw_value = pcf.get("pcfExcludingBiogenicUptake")
    value_key = "pcfExcludingBiogenicUptake"
    if raw_value is None:
        raw_value, value_key = pcf.get("pCfExcludingBiogenic"), "pCfExcludingBiogenic"
    value = _number(raw_value)
    if value is None:
        problems.append("No kgCO2e value (pcfExcludingBiogenicUptake) in the PACT file")
    else:
        reason = f"{src} {value_key}"
        if amount not in (None, 0, 1):
            value = value / amount
            reason += f" ÷ declaredUnitAmount {amount:g}"
        if per_engine != 1.0 and hit:
            value = value / per_engine
            reason += f", per {hit[0]}"
        f["value_kgco2e"] = Field(round(value, 9), 100 if not problems else 40, reason + " (kg CO2e per declared unit, excluding biogenic uptake)", "pact")

    boundary = boundary_from(pcf.get("boundaryProcessesDescription"))
    if boundary:
        f["boundary"] = Field(boundary, 90, f"{src} boundaryProcessesDescription", "pact")
    else:
        f["boundary"] = Field("cradle_to_gate", 70, "PACT product footprints are cradle-to-gate by definition; the file names no other boundary", "pact")

    standards = pcf.get("crossSectoralStandards") or pcf.get("crossSectoralStandardsUsed") or []
    if isinstance(standards, list) and standards:
        joined = "; ".join(_text(s) for s in standards)
        code = standard_from(joined)
        f["standard"] = Field(code or joined, 100 if code else 60, f"{src} crossSectoralStandards: {joined}", "pact")

    gwps = pcf.get("ipccCharacterizationFactors") or pcf.get("characterizationFactors") or []
    if isinstance(gwps, str):
        gwps = [gwps]
    if isinstance(gwps, list) and gwps:
        gwp = gwp_from(" ".join(_text(g) for g in gwps))
        if gwp:
            f["gwp_set"] = Field(gwp, 100, f"{src} ipccCharacterizationFactors", "pact")

    for key, pact_key, holder in (
        ("reference_start", "referencePeriodStart", pcf),
        ("reference_end", "referencePeriodEnd", pcf),
        ("valid_from", "validityPeriodStart", data),
        ("valid_to", "validityPeriodEnd", data),
    ):
        iso = to_iso_date(holder.get(pact_key))
        if iso:
            f[key] = Field(iso, 100, f"{src} {pact_key}", "pact")
    if f["valid_to"].value is None and f["reference_end"].value:
        end = date.fromisoformat(f["reference_end"].value)
        f["valid_to"] = Field(_add_years(end, 3).isoformat(), 70, "PACT default validity: three years after the reference period end", "pact")

    assurance = pcf.get("assurance")
    if isinstance(assurance, dict):
        provider = _text(assurance.get("providerName"))
        assured = assurance.get("assurance", True)
        if provider and assured is not False:
            f["verifier"] = Field(provider, 100, f"{src} assurance.providerName", "pact")
        elif assured is False:
            f["verifier"] = Field(None, 90, "PACT assurance: not assured", "pact")

    if data.get("status") == "Deprecated":
        warnings.append("The PACT file is marked Deprecated: ask the supplier for the current version")
    return f, warnings, problems


# ------------------------------------------------------------------ PDF ---

AI_DECLARATION_PROMPT = """\
You read a supplier's product carbon footprint (PCF) or environmental product declaration (EPD) from a document's text.
Return ONLY a JSON object with these keys, each as {"value": <value or null>, "confidence": <0-100>, "quote": "<the words it came from, or null>"}:
"product": the product the figure is for (string);
"supplier": the company that issued it (string);
"value": the global warming potential / carbon footprint number as written (number) — prefer the cradle-to-gate (A1-A3) GWP-total or GWP-fossil figure for the whole declared unit;
"value_unit": the mass of CO2e the number is written in, as written (e.g. "kg CO2e", "t CO2-eq");
"per_quantity": how many declared units the number covers (number, usually 1);
"per_unit": the declared/functional unit as written (e.g. "kg", "tonne", "piece", "m2");
"boundary": the system boundary as written ("cradle-to-gate", "cradle-to-grave", "gate-to-gate", "A1-A3", ...);
"standard": the standard(s) it follows as written (ISO 14067, EN 15804, GHG Protocol Product Standard, PAS 2050, ...);
"gwp": the IPCC assessment report of the characterisation factors as written (e.g. "AR5", "AR6 GWP100");
"reference_start" and "reference_end": the data/reference period, as YYYY-MM-DD;
"valid_from" and "valid_to": the declaration's validity period, as YYYY-MM-DD;
"verifier": the third-party verifier or programme operator that verified it (string).
Copy numbers and units exactly as written; never convert them. Use null when the document does not say.
"""


def _ai_field(item: Any, max_conf: int = AI_MAX_CONFIDENCE) -> tuple[Any, int, str]:
    if not isinstance(item, dict):
        return item, 50, ""
    try:
        conf = int(max(0, min(max_conf, float(item.get("confidence", 50)))))
    except (TypeError, ValueError):
        conf = 50
    quote = _text(item.get("quote"))[:160]
    return item.get("value"), conf, (f"“{quote}”" if quote else "")


def _read_text(text: str, call_llm: Callable[[list[dict]], str]) -> tuple[dict[str, Field], list[str], list[str]]:
    from app.services.llm import LLMError, _parse_json_object

    messages = [
        {"role": "system", "content": AI_DECLARATION_PROMPT},
        {"role": "user", "content": text[:PDF_TEXT_LIMIT]},
    ]
    try:
        data = _parse_json_object(call_llm(messages))
    except LLMError as e:
        raise DeclarationError(f"The AI couldn't read the document: {e}")
    except Exception as e:  # SDK, network or parsing failures are the caller's 422, not a 500
        logger.warning("Declaration read failed: %s: %s", type(e).__name__, e)
        raise DeclarationError("The AI couldn't read the document; try again or type the values.")
    if not isinstance(data, dict):
        raise DeclarationError("The AI couldn't find a declaration in the document.")

    warnings: list[str] = []
    problems: list[str] = []
    f: dict[str, Field] = {k: _empty() for k in FIELDS}

    def quoted(key: str) -> tuple[Any, int, str]:
        v, c, q = _ai_field(data.get(key))
        return v, c, (f"Read by the AI from {q}" if q else "Read by the AI from the document text")

    for key, out in (("product", "product_name"), ("supplier", "supplier_name"), ("verifier", "verifier")):
        v, c, reason = quoted(key)
        if _text(v):
            f[out] = Field(_text(v), c, reason, "ai")

    # Value: number × CO2e mass ÷ (per_quantity × declared unit).
    v, c, reason = quoted("value")
    value = _number(v)
    unit_written, uc, unit_reason = quoted("per_unit")
    unit = to_pcf_unit(unit_written)
    per_qty = _number(_ai_field(data.get("per_quantity"))[0]) or 1.0
    if isinstance(unit, PcfUnit):
        f["declared_unit"] = Field(unit.unit, uc, f"{unit_reason}; {unit.reason}", "ai")
    elif _text(unit_written):
        f["declared_unit"] = Field(_text(unit_written), min(uc, 30), f"{unit_reason}; {unit.reason}", "ai")
        problems.append(unit.reason)
    else:
        problems.append("No declared unit found")
    if value is None:
        problems.append("No kgCO2e value found")
    else:
        notes = []
        mult = co2e_multiplier(_ai_field(data.get("value_unit"))[0])
        if mult:
            value *= mult[0]
            if mult[0] != 1.0:
                notes.append(mult[1])
        else:
            notes.append("the document's CO2e mass unit wasn't recognised, so the number is taken as kg CO2e")
            c = min(c, 40)
        if per_qty not in (0, 1):
            value /= per_qty
            notes.append(f"÷ {per_qty:g} declared units")
        if isinstance(unit, PcfUnit) and unit.per_written != 1.0:
            value /= unit.per_written
            notes.append(f"per {unit.unit}")
        if problems:
            c = min(c, 30)
        f["value_kgco2e"] = Field(round(value, 9), c, "; ".join([reason] + notes), "ai")

    v, c, reason = quoted("boundary")
    boundary = boundary_from(v)
    if boundary:
        f["boundary"] = Field(boundary, c, reason, "ai")
    elif _text(v):
        f["boundary"] = Field(None, 0, f"Boundary “{_text(v)}” isn't cradle-to-gate, cradle-to-grave or gate-to-gate", "ai")
        warnings.append(f"The document's boundary “{_text(v)}” wasn't recognised")

    v, c, reason = quoted("standard")
    if _text(v):
        code = standard_from(v)
        f["standard"] = Field(code or _text(v), c if code else min(c, 50), reason, "ai")

    v, c, reason = quoted("gwp")
    gwp = gwp_from(v)
    if gwp:
        f["gwp_set"] = Field(gwp, c, reason, "ai")

    for key in ("reference_start", "reference_end", "valid_from", "valid_to"):
        v, c, reason = quoted(key)
        iso = to_iso_date(v)
        if iso:
            f[key] = Field(iso, c, reason, "ai")
        elif _text(v):
            f[key] = Field(None, 0, f"Date “{_text(v)}” couldn't be read", "ai")
            warnings.append(f"{key.replace('_', ' ')} “{_text(v)}” couldn't be read as a date")
    return f, warnings, problems


# -------------------------------------------------------------- read ---


def _checks(f: dict[str, Field], expected_boundary: Optional[str], expected_unit: Optional[str], warnings: list[str]) -> tuple[bool, bool]:
    boundary_mismatch = False
    unit_mismatch = False
    b = f["boundary"].value
    if expected_boundary and b and b != expected_boundary:
        boundary_mismatch = True
        warnings.append(
            f"The declaration is {b.replace('_', '-')} but the footprint is {expected_boundary.replace('_', '-')}: "
            "the figure covers different stages and can't be accepted as it is"
        )
    u = f["declared_unit"].value
    if expected_unit and u in PCF_UNITS and u != expected_unit:
        if {u, expected_unit} == {"kg", "t"}:
            val = f["value_kgco2e"]
            if val.value is not None:
                factor = 1000.0 if expected_unit == "kg" else 1 / 1000.0
                f["value_kgco2e"] = Field(round(val.value / factor, 9), val.confidence, f"{val.reason}; converted from per {u} to per {expected_unit}", val.by)
            f["declared_unit"] = Field(expected_unit, f["declared_unit"].confidence, f"{f['declared_unit'].reason}; converted to {expected_unit}", f["declared_unit"].by)
        else:
            unit_mismatch = True
            warnings.append(f"The declaration is per {u} but the input is per {expected_unit}; convert it or ask the supplier for a figure per {expected_unit}")
    if f["valid_to"].value:
        try:
            if date.fromisoformat(f["valid_to"].value) < date.today():
                warnings.append(f"The declaration expired on {f['valid_to'].value}")
        except ValueError:
            pass
    return boundary_mismatch, unit_mismatch


def read_declaration(
    content: bytes,
    filename: str,
    expected_boundary: Optional[str] = "cradle_to_gate",
    expected_unit: Optional[str] = None,
    call_llm: Optional[Callable[[list[dict]], str]] = None,
    extract_text: Optional[Callable[[bytes, str], str]] = None,
) -> dict[str, Any]:
    """A supplier PCF/EPD (.pdf) or PACT product footprint (.json) → reviewable fields."""
    name = (filename or "").lower()
    ai_used = False
    if name.endswith(".json"):
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeclarationError("The file isn't valid JSON.")
        if isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"] and _is_pact(data["data"][0]):
            data = data["data"][0]  # a PACT API listing: take the first footprint
        if not _is_pact(data):
            raise DeclarationError("The JSON isn't a PACT product footprint (no “pcf” object with a declared unit).")
        kind = "pact"
        fields, warnings, problems = _read_pact(data)
    elif name.endswith(".pdf"):
        if call_llm is None or extract_text is None:
            raise DeclarationError("Reading a PDF needs the AI, which isn't available here.")
        text = extract_text(content, filename)
        if not _text(text):
            raise DeclarationError("No text could be read from the PDF.")
        kind = "pdf"
        ai_used = True
        fields, warnings, problems = _read_text(text, call_llm)
    else:
        raise DeclarationError("Use a .pdf or a PACT .json file.")

    boundary_mismatch, unit_mismatch = _checks(fields, expected_boundary, expected_unit, warnings)
    low = [k for k, v in fields.items() if v.value is not None and v.confidence < LOW_CONFIDENCE]
    return {
        "file_name": filename,
        "kind": kind,
        "fields": {k: asdict(v) for k, v in fields.items()},
        "boundary_mismatch": boundary_mismatch,
        "unit_mismatch": unit_mismatch,
        "low_confidence_fields": low,
        "warnings": warnings,
        "problems": problems,
        "ai_used": ai_used,
    }
