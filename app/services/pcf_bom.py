"""PCF BOM reader (E2 2/3): an ERP export or spec sheet → PcfInput-shaped lines.

A bill of materials arrives as .xlsx or .csv (read like the factor sheet, any
layout) or as a .pdf (text out of the invoice OCR path, lines out of the LLM).
Every line comes back with the body C02 puts in ``PUT /pcf/studies/:id/inputs``
(stage, name, quantity, unit, supplier, origin, recycled share), a
``confidence`` (0–100), a ``reason``, warnings and the problems ESG-lite would
refuse. Quantities are converted to the six PCF units and scaled to one
declared unit; a unit that can't be placed is a problem, never a guess.
Nothing is saved to ESG-lite: the person confirms the lines in the builder.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.services.pcf_sheet import (
    ColumnMatch,
    Layout,
    SheetError,
    _blank,
    _cell_text,
    _number,
    ai_mapping,
    infer_group,
    read_table,
)
from app.services.pcf_units import PcfUnit, to_pcf_unit

logger = logging.getLogger(__name__)

STAGES = ("A1", "A2", "A3_energy", "A3_packaging", "A3_waste")
SAMPLE_ROWS = 5
MAX_PDF_LINES = 400
PDF_TEXT_LIMIT = 24000

FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("quantity", "Quantity", True),
    ("name", "Material", True),
    ("unit", "Unit", True),
    ("stage", "Stage", False),
    ("supplier_name", "Supplier", False),
    ("origin_country", "Origin", False),
    ("recycled_share_pct", "Recycled share %", False),
    ("code", "Item code", False),
)
FIELD_LABEL = {f: label for f, label, _ in FIELDS}

SYNONYMS: dict[str, list[str]] = {
    "name": ["material", "materialname", "description", "item", "itemdescription", "component", "part", "partname", "name", "rawmaterial", "input", "ingredient", "product", "article"],
    "quantity": ["quantity", "qty", "amount", "consumption", "usage", "input", "requirement", "net", "netqty", "qtyperunit", "quantityperunit", "perunit", "mass", "weight", "netweight", "volume"],
    "unit": ["unit", "units", "uom", "unitofmeasure", "measure", "qtyunit", "quantityunit"],
    "stage": ["stage", "lifecyclestage", "lcastage", "module", "phase", "category", "type", "inputtype"],
    "supplier_name": ["supplier", "suppliername", "vendor", "vendorname", "manufacturer", "source"],
    "origin_country": ["origin", "origincountry", "countryoforigin", "country", "region", "location", "madein"],
    "recycled_share_pct": ["recycledshare", "recycled", "recycledcontent", "recycledpct", "recycledpercent", "secondarycontent", "scrapshare"],
    "code": ["code", "itemcode", "partnumber", "partno", "sku", "materialcode", "articleno", "articlenumber", "id", "ref"],
}

LAYOUT = Layout(FIELDS, SYNONYMS, ("name", "quantity"))

# Words in a stage cell → PCF stage. Anything else is A1 unless the group says packaging.
STAGE_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("A3_packaging", ("packaging", "packing", "pack", "a3 packaging", "a3_packaging")),
    ("A3_waste", ("waste", "scrap", "disposal", "a3 waste", "a3_waste")),
    ("A3_energy", ("energy", "electricity", "fuel", "a3 energy", "a3_energy")),
    ("A2", ("transport", "freight", "inbound", "logistics", "a2")),
    ("A1", ("material", "raw", "a1", "input", "component")),
]

AI_BOM_PROMPT = """\
You read a bill of materials out of a document's text for a product carbon footprint.
Return ONLY a JSON object: {"lines": [{"material": "<name as written>", "quantity": <number>, "unit": "<unit as written>", "supplier": "<or null>", "origin": "<country or null>", "confidence": <0-100>, "reason": "<one short sentence>"}]}
List every material, packaging and waste line with a quantity. Keep units as written; never convert them.
Leave out prices, totals, headings and lines without a quantity. If the document gives quantities per batch or per order, say so in "batch" as {"quantity": <number>, "unit": "<unit>"} at the top level, else omit it.
"""


@dataclass
class BomLine:
    line: int
    values: dict[str, Any]
    confidence: int
    reason: str
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    by: str = "sheet"  # "sheet" | "ai"


def stage_from(text: str, name: str) -> tuple[str, Optional[str]]:
    """(stage, the word that decided it). Defaults to A1; packaging names go to A3 packaging."""
    low = f" {text.lower()} "
    for stage, words in STAGE_WORDS:
        for w in words:
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
                return stage, w
    group = infer_group(name)
    if group and group[0] == "packaging":
        return "A3_packaging", group[1]
    return "A1", None


def unit_in_header(header: str) -> Optional[PcfUnit]:
    """The unit a quantity header names: “Qty (kg)”, “Mass [t]”, “Quantity in g”, “Net weight kg”."""
    text = str(header or "")
    candidates = re.findall(r"[(\[]([^)\]]+)[)\]]", text)
    m = re.search(r"\b(?:in|per)\s+([^\s()\[\]]+)\s*$", text, re.I)
    if m:
        candidates.append(m.group(1))
    words = re.sub(r"[(\[].*?[)\]]", " ", text).split()
    if len(words) > 1:
        candidates.append(words[-1])
    for c in candidates:
        u = to_pcf_unit(c)
        if isinstance(u, PcfUnit):
            return u
    return None


def _percent(v: Any) -> Optional[float]:
    n = _number(str(v).replace("%", "") if isinstance(v, str) else v)
    if n is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < n <= 1:
        return n * 100  # 0.2 in a percent column is 20%
    return n


def _line_from_sheet(
    line: int,
    raw: dict[str, Any],
    mapping: dict[str, ColumnMatch],
    sheet_unit: Optional[PcfUnit],
    batch_units: float,
) -> BomLine:
    get = lambda f: raw.get(mapping[f].header) if f in mapping else None  # noqa: E731
    v: dict[str, Any] = {}
    warnings: list[str] = []
    problems: list[str] = []
    notes: list[str] = []
    confs = [mapping[f].confidence for f in LAYOUT.needs if f in mapping]

    name = _cell_text(get("name"))
    code = _cell_text(get("code"))
    v["name"] = name
    if not name:
        problems.append("No material name")
    elif code and code.lower() not in name.lower():
        v["name"] = f"{name} ({code})"

    stage, word = stage_from(_cell_text(get("stage")), name)
    v["stage"] = stage
    if word:
        notes.append(f"stage {stage} from “{word}”")

    qty = _number(get("quantity"))
    if qty is None:
        problems.append("No quantity" if _blank(get("quantity")) else f"Quantity “{_cell_text(get('quantity'))}” is not a number")
    elif qty < 0:
        problems.append("Quantity is negative")

    unit_cell = get("unit")
    unit = to_pcf_unit(unit_cell) if not _blank(unit_cell) else (sheet_unit or to_pcf_unit(""))
    if isinstance(unit, PcfUnit):
        v["unit"] = unit.unit
        if unit.per_written != 1.0 and qty is not None:
            qty = qty * unit.per_written
            notes.append(unit.reason)
        elif _blank(unit_cell) and sheet_unit:
            notes.append(sheet_unit.reason)
    else:
        v["unit"] = _cell_text(unit_cell)
        problems.append(unit.reason if not _blank(unit_cell) else "No unit; the quantity column header names none either")

    if qty is not None and batch_units != 1:
        qty = qty / batch_units
        notes.append(f"per declared unit (÷{batch_units:g})")
    v["quantity"] = round(qty, 9) if qty is not None else None
    if qty == 0:
        warnings.append("Quantity is zero; the line adds nothing")

    sup = _cell_text(get("supplier_name"))
    v["supplier_name"] = sup or None
    origin = _cell_text(get("origin_country"))
    v["origin_country"] = origin or None
    rec = _percent(get("recycled_share_pct")) if not _blank(get("recycled_share_pct")) else None
    if rec is not None and not 0 <= rec <= 100:
        warnings.append(f"Recycled share {rec:g}% is outside 0–100; left blank")
        rec = None
    v["recycled_share_pct"] = rec if rec is not None else 0
    v["data_type"] = "secondary"

    confidence = min(confs) if confs else 0
    if problems:
        confidence = min(confidence, 30)
    reason = f"{FIELD_LABEL['name']} and quantity from the sheet columns" + (f"; {'; '.join(notes)}" if notes else "")
    return BomLine(line, v, confidence, reason, warnings, problems)


def _lines_from_text(
    text: str,
    call_llm: Callable[[list[dict]], str],
    batch_units: float,
) -> tuple[list[BomLine], list[str]]:
    from app.services.llm import _parse_json_object

    messages = [
        {"role": "system", "content": AI_BOM_PROMPT},
        {"role": "user", "content": text[:PDF_TEXT_LIMIT]},
    ]
    data = _parse_json_object(call_llm(messages))
    raw_lines = data.get("lines") if isinstance(data, dict) else data
    warnings: list[str] = []
    if not isinstance(raw_lines, list):
        raise SheetError("Couldn't find a bill of materials in the document.")
    batch = data.get("batch") if isinstance(data, dict) else None
    if isinstance(batch, dict) and batch_units == 1 and _number(batch.get("quantity")) not in (None, 0, 1):
        warnings.append(
            f"The document gives quantities per {_number(batch.get('quantity')):g} {batch.get('unit') or 'units'}; "
            "set the batch size to read them per declared unit."
        )
    out: list[BomLine] = []
    for i, item in enumerate(raw_lines[:MAX_PDF_LINES], start=1):
        if not isinstance(item, dict):
            continue
        v: dict[str, Any] = {}
        problems: list[str] = []
        notes: list[str] = []
        name = _cell_text(item.get("material"))
        v["name"] = name
        if not name:
            problems.append("No material name")
        stage, word = stage_from("", name)
        v["stage"] = stage
        qty = _number(item.get("quantity"))
        if qty is None:
            problems.append("No quantity")
        unit = to_pcf_unit(item.get("unit"))
        if isinstance(unit, PcfUnit):
            v["unit"] = unit.unit
            if unit.per_written != 1.0 and qty is not None:
                qty = qty * unit.per_written
                notes.append(unit.reason)
        else:
            v["unit"] = _cell_text(item.get("unit"))
            problems.append(unit.reason)
        if qty is not None and batch_units != 1:
            qty = qty / batch_units
            notes.append(f"per declared unit (÷{batch_units:g})")
        v["quantity"] = round(qty, 9) if qty is not None else None
        v["supplier_name"] = _cell_text(item.get("supplier")) or None
        v["origin_country"] = _cell_text(item.get("origin")) or None
        v["recycled_share_pct"] = 0
        v["data_type"] = "secondary"
        try:
            conf = int(max(0, min(85, float(item.get("confidence", 50)))))  # read by AI from text: never certain
        except (TypeError, ValueError):
            conf = 50
        if problems:
            conf = min(conf, 30)
        reason = str(item.get("reason") or "Read from the document text by the AI").strip()[:200]
        if notes:
            reason = f"{reason}; {'; '.join(notes)}"
        out.append(BomLine(i, v, conf, reason, [], problems, by="ai"))
    if not out:
        raise SheetError("Couldn't find a bill of materials in the document.")
    return out, warnings


def _mass_check(lines: list[BomLine], mass_per_unit_kg: Optional[float]) -> Optional[str]:
    """A1 mass per declared unit against the product's mass, when both are known."""
    if not mass_per_unit_kg or mass_per_unit_kg <= 0:
        return None
    kg = 0.0
    for l in lines:
        if l.values.get("stage") != "A1" or l.values.get("quantity") is None or l.problems:
            continue
        u = l.values.get("unit")
        if u == "kg":
            kg += l.values["quantity"]
        elif u == "t":
            kg += l.values["quantity"] * 1000
    if kg == 0:
        return None
    ratio = kg / mass_per_unit_kg
    if ratio < 0.8 or ratio > 1.5:
        return (
            f"The A1 materials add up to {kg:,.4g} kg per declared unit against a product mass of {mass_per_unit_kg:,.4g} kg; "
            "check the batch size or the units."
        )
    return None


def read_bom(
    content: bytes,
    filename: str,
    sheet_name: Optional[str] = None,
    mapping_override: Optional[dict[str, Optional[str]]] = None,
    batch_units: float = 1.0,
    mass_per_unit_kg: Optional[float] = None,
    call_llm: Optional[Callable[[list[dict]], str]] = None,
    extract_text: Optional[Callable[[bytes, str], str]] = None,
) -> dict[str, Any]:
    """The BOM as PcfInput-shaped lines. ``batch_units`` is how many declared units the sheet's quantities cover."""
    if not math.isfinite(batch_units) or batch_units <= 0:
        raise SheetError("The batch size must be a number above zero.")
    if mass_per_unit_kg is not None and not math.isfinite(mass_per_unit_kg):
        mass_per_unit_kg = None
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    warnings: list[str] = []
    ai_used = False

    if ext == "pdf":
        if call_llm is None or extract_text is None:
            raise SheetError("Reading a PDF needs the AI reader, which isn't available right now.")
        text = extract_text(content, filename)
        if not text or len(text.strip()) < 20:
            raise SheetError("Couldn't read any text from the PDF.")
        try:
            lines, text_warnings = _lines_from_text(text, call_llm, batch_units)
        except SheetError:
            raise
        except Exception as e:  # the LLM timed out or answered with something that isn't a BOM
            logger.warning("AI BOM reading failed", exc_info=True)
            raise SheetError("The AI reader couldn't read a bill of materials out of this PDF; try an .xlsx or .csv export.") from e
        warnings.extend(text_warnings)
        ai_used = True
        headers: list[str] = []
        mapping: dict[str, ColumnMatch] = {}
        sheet_names: list[str] = []
        picked_sheet = None
        header_line = 0
    else:
        if ext not in ("xlsx", "csv"):
            raise SheetError("Use an .xlsx, .csv or .pdf file. Save an .xls file as .xlsx first.")
        grid, header_idx, headers, data, mapping, warnings = read_table(content, filename, sheet_name, LAYOUT, mapping_override)
        missing = [f for f in LAYOUT.needs if f not in mapping]
        if missing and call_llm is not None:
            free = [h for h in headers if h not in {m.header for m in mapping.values()}]
            try:
                found = ai_mapping(
                    free,
                    [r for _, r in data[:SAMPLE_ROWS]],
                    missing,
                    call_llm,
                    labels=FIELD_LABEL,
                    what="a bill of materials line (one material of a product)",
                    hint="quantity is the numeric amount of the material used; never a price, a total or a code.",
                )
                mapping.update(found)
                ai_used = bool(found)
            except Exception:
                logger.warning("AI column mapping failed; using header names only", exc_info=True)
                warnings.append("The AI sheet reader is unavailable right now; columns were matched by header name only.")
        sheet_unit = None
        if "unit" not in mapping and "quantity" in mapping:
            u = unit_in_header(mapping["quantity"].header)
            if u:
                sheet_unit = PcfUnit(u.unit, u.per_written, f"No unit column; every quantity is in {u.unit}, from the header “{mapping['quantity'].header}”")
        lines = [_line_from_sheet(line, raw, mapping, sheet_unit, batch_units) for line, raw in data]
        for f in LAYOUT.needs:
            if f not in mapping:
                warnings.append(f"No column for {FIELD_LABEL[f].lower()}; pick one.")
        if "unit" not in mapping and sheet_unit is None:
            warnings.append("No unit column and the quantity header names no unit; every line needs a unit.")
        sheet_names, picked_sheet, header_line = grid.sheet_names, grid.sheet_name, header_idx + 1

    mass_note = _mass_check(lines, mass_per_unit_kg)
    if mass_note:
        warnings.append(mass_note)

    return {
        "file_name": filename,
        "sheet_names": sheet_names,
        "sheet_name": picked_sheet,
        "header_line": header_line,
        "headers": headers,
        "mapping": {f: m.__dict__ for f, m in mapping.items()},
        "batch_units": batch_units,
        "lines": [l.__dict__ for l in lines],
        "total_lines": len(lines),
        "lines_with_problems": sum(1 for l in lines if l.problems),
        "low_confidence_lines": sum(1 for l in lines if l.confidence < 60),
        "warnings": warnings,
        "ai_used": ai_used,
    }


def json_safe(d: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(d, default=str))
