import io
import logging
import os
import re
from pathlib import Path
from typing import Optional

import openpyxl
import pandas as pd
import requests

from app.services.llm import _call_openrouter, _parse_json_object
from app.schemas.emission_factor import (
    SpreadsheetSchema,
    EmissionFactorRecord,
    ParseExcelResponse,
    DbCategory,
    CategorySuggestion,
)
from app.core.database import get_connection, release_connection, get_uploaded_document_by_id, fetch_emission_factor, bulk_insert_emissions_with_conn, fetch_column_config

logger = logging.getLogger(__name__)


# ===========================================================================
# PART 1: Emission Factor Excel Parser (LLM-assisted schema detection)
# ===========================================================================

# ---------------------------------------------------------------------------
# Unit normalisation — strip qualifiers like "of material", "of waste" etc.
# "ton of material" → "ton",  "per passenger.km" → "passenger.km"
# ---------------------------------------------------------------------------

_UNIT_STRIP_PATTERNS = [
    # "X of <something>" — keep only X
    re.compile(r"^(.+?)\s+of\s+\w.*$", re.IGNORECASE),
    # leading "per " — drop it
    re.compile(r"^per\s+(.+)$", re.IGNORECASE),
]


_UNIT_ALIASES: dict[str, str] = {
    "tonnes": "tonne",
    "tons": "ton",
    "litres": "litre",
    "liters": "litre",
    "liter": "litre",
    "gallons": "gallon",
    "kilo litre": "kl",
    "kilolitre": "kl",
    "kiloliter": "kl",
    "cubic metre": "cubic meter",
    "m3": "cubic meter",
    "m³": "cubic meter",
    "kilogram": "kg",
    "kilograms": "kg",
    "kgs": "kg",
    "gram": "g",
    "grams": "g",
    "pound": "lb",
    "pounds": "lb",
    "lbs": "lb",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "miles": "mile",
    "mi": "mile",
}


def _normalise_unit(raw: str) -> str:
    """
    Clean a denominator unit extracted from an emission factor sheet.

    Examples:
        "ton of material"   → "ton"
        "Tonnes"            → "tonne"
        "per passenger.km"  → "passenger.km"
        "KWH"              → "kwh"
        "Litre"            → "litre"
    """
    text = raw.strip()
    # Strip parentheses (UOM cells often have wrapping parens like "(kg CO2 e/ km)")
    text = text.replace("(", "").replace(")", "").strip()
    for pat in _UNIT_STRIP_PATTERNS:
        m = pat.match(text)
        if m:
            text = m.group(1).strip()
    lower = text.lower()
    return _UNIT_ALIASES.get(lower, lower)


# ---------------------------------------------------------------------------
# System prompt for Pass 1: LLM schema detection
# ---------------------------------------------------------------------------

SCHEMA_DETECTION_PROMPT = """\
You are an expert at analyzing Excel spreadsheet structures for emission factor data.

You will receive the first ~20 rows of an Excel spreadsheet, represented as a list of
rows where each row is a list of (cell_value, column_index) tuples for non-empty cells.
Column indices are 1-based (A=1, B=2, ...).

Your task is to identify the STRUCTURE of this spreadsheet so that a program can
deterministically extract all data rows. Analyze the header rows to determine:

1. LAYOUT TYPE — one of three patterns:
   - "simple": One factor value per year per row.
     Each year has exactly 1 value column.
   - "sub_columns": Multiple sub-columns per year (e.g. Direct, WTT, Total).
     Each year repeats the same set of sub-columns. The program will pick one
     sub-column (typically "Total") as the factor value.
   - "disposal_pivot": Multiple disposal/treatment method columns per year
     (e.g. Re-use, Open loop, Closed loop, Combustion, Composting, Landfilled,
     Anaerobically digested). Each disposal column × each data row = a separate
     emission factor record.

2. DESCRIPTOR COLUMNS — which columns contain text descriptors that together form
   the emission_category_name. These are text columns to the LEFT of the numeric
   factor data. Report them in left-to-right order. Exclude:
   - Broad "Category" or "Method of calculation" columns that are the same for all rows
   - Source / UOM columns
   Only include columns whose values differentiate emission factor records.

3. PARENT CATEGORY COLUMN — an optional grouping column (e.g. "Stationary Combustion",
   "Mobile Combustion", "Road", "Air"). Rows with a value ONLY in this column and no
   numeric data are group headers. Set `include_parent_in_name` to true ONLY if the
   same descriptor values appear under different parents and need disambiguation
   (e.g. "Diesel" under both "Stationary Combustion" and "Mobile Combustion").

4. UNIT COLUMN — which column (if any) contains the unit of measurement (UOM).

5. SOURCE COLUMN — which column (if any) contains the data source (e.g. "DEFRA").

6. DATA START ROW — the first row number containing actual data values (not headers).

7. YEAR COLUMNS — for each year detected:
   - "simple": provide `value_column` (the column index).
   - "sub_columns": provide `sub_columns` (list of {name, column_index}) and
     `primary_sub_column` (name of the sub-column to use, usually "Total").
   - "disposal_pivot": provide `disposal_columns` (list of {name, column_index}).

Return ONLY a JSON object with this exact structure (no extra text):
{
  "layout_type": "simple" | "sub_columns" | "disposal_pivot",
  "descriptor_columns": [
    {"column_index": <int>, "header_name": "<string>"}
  ],
  "parent_category_column": {"column_index": <int>, "header_name": "<string>"} | null,
  "include_parent_in_name": true | false,
  "source_column": {"column_index": <int>, "header_name": "<string>"} | null,
  "unit_column": {"column_index": <int>, "header_name": "<string>"} | null,
  "data_start_row": <int>,
  "years": [
    {
      "year": <int>,
      "value_column": <int or null>,
      "sub_columns": [{"name": "<string>", "column_index": <int>}] | null,
      "primary_sub_column": "<string>" | null,
      "disposal_columns": [{"name": "<string>", "column_index": <int>}] | null
    }
  ],
  "descriptor_join_separator": " - ",
  "notes": "<any observations about the data>"
}
"""


# ---------------------------------------------------------------------------
# Pass 1 helpers
# ---------------------------------------------------------------------------

def _read_header_rows(ws, max_rows: int = 20) -> str:
    """Read first N rows of worksheet, format as (value, column_index) tuples."""
    lines: list[str] = []
    for row in ws.iter_rows(
        min_row=1, max_row=min(max_rows, ws.max_row), values_only=False
    ):
        row_num = row[0].row
        cells = [
            (cell.value, cell.column)
            for cell in row
            if cell.value is not None
        ]
        if cells:
            lines.append(f"Row {row_num}: {cells}")
    return "\n".join(lines)


def _detect_schema(header_text: str) -> SpreadsheetSchema:
    """Pass 1: Send header rows to LLM for structure detection."""
    messages = [
        {"role": "system", "content": SCHEMA_DETECTION_PROMPT},
        {"role": "user", "content": f"SPREADSHEET ROWS:\n{header_text}"},
    ]
    logger.info("Pass 1: Calling LLM for schema detection")
    raw_response = _call_openrouter(messages)
    schema_dict = _parse_json_object(raw_response)
    logger.info(f"Pass 1: LLM returned schema: layout_type={schema_dict.get('layout_type')}")
    return SpreadsheetSchema(**schema_dict)


# ---------------------------------------------------------------------------
# Pass 2: Deterministic extraction
# ---------------------------------------------------------------------------

def _parse_numeric(value, precision: int = 6) -> Optional[float]:
    """Safely convert a cell value to float, returning None for non-numeric.

    Rounds to *precision* decimal places to strip floating-point noise
    that arises from Excel formula caching (e.g. 0.74994 stored as
    0.7499399999999999).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, bool):
            return None
        return round(float(value), precision)
    s = str(value).strip()
    if s in ("", "-", "N/A", "n/a", "NA", "na", "#N/A", "#REF!", "#VALUE!"):
        return None
    try:
        return round(float(s), precision)
    except ValueError:
        return None


def _extract_factors(
    ws, schema: SpreadsheetSchema
) -> tuple[list[EmissionFactorRecord], list[str]]:
    """
    Pass 2: Use detected schema to programmatically read all data rows.
    No LLM involved — purely deterministic openpyxl reads.
    """
    factors: list[EmissionFactorRecord] = []
    warnings: list[str] = []

    current_parent: Optional[str] = None
    last_unit: Optional[str] = None
    last_source: Optional[str] = None
    # Forward-fill for merged descriptor cells: column_index -> last non-empty value
    last_descriptors: dict[int, str] = {}

    for row in ws.iter_rows(
        min_row=schema.data_start_row, max_row=ws.max_row, values_only=False
    ):
        row_num = row[0].row
        # Build a column_index -> cell_value lookup
        cells: dict[int, any] = {cell.column: cell.value for cell in row}

        # --- Track parent category (for group-header rows) ---
        if schema.parent_category_column:
            parent_val = cells.get(schema.parent_category_column.column_index)
            if parent_val is not None and str(parent_val).strip():
                current_parent = str(parent_val).strip()

        # --- Build emission_category_name from descriptors ---
        # Forward-fill handles merged cells: when a cell is empty, use the
        # last value for that column.  When a cell has a new value, reset
        # all columns to its right so stale child-level values don't leak.
        descriptor_parts: list[str] = []
        if schema.include_parent_in_name and current_parent:
            descriptor_parts.append(current_parent)

        has_any_explicit = False  # track if row has at least one real cell
        for i, desc_col in enumerate(schema.descriptor_columns):
            val = cells.get(desc_col.column_index)
            val_str = str(val).strip() if val is not None else ""

            if val_str and val_str != "-":
                # Explicit value — update forward-fill and reset downstream
                has_any_explicit = True
                last_descriptors[desc_col.column_index] = val_str
                for j in range(i + 1, len(schema.descriptor_columns)):
                    last_descriptors.pop(
                        schema.descriptor_columns[j].column_index, None
                    )
                descriptor_parts.append(val_str)
            elif desc_col.column_index in last_descriptors:
                # Empty / merged cell — use forward-filled value
                descriptor_parts.append(last_descriptors[desc_col.column_index])
            # else: truly empty and no previous value, skip

        if not descriptor_parts or (
            not has_any_explicit
            and not schema.include_parent_in_name
        ):
            continue  # Skip empty / group-header rows with no descriptors

        emission_category_name = schema.descriptor_join_separator.join(
            descriptor_parts
        )

        # --- Source and unit ---
        source: Optional[str] = None
        if schema.source_column:
            src_val = cells.get(schema.source_column.column_index)
            if src_val is not None and str(src_val).strip():
                source = str(src_val).strip()
                last_source = source
            elif last_source:
                source = last_source

        denominator_unit: Optional[str] = None
        if schema.unit_column:
            unit_val = cells.get(schema.unit_column.column_index)
            if unit_val is not None and str(unit_val).strip():
                raw_unit = str(unit_val).strip()
                # Extract just the denominator (e.g. "KgCO2e/USD" → "USD")
                denominator_unit = raw_unit.split("/", 1)[1].strip() if "/" in raw_unit else raw_unit
                # Normalise: "ton of material" → "ton", "per passenger.km" → "passenger.km"
                denominator_unit = _normalise_unit(denominator_unit)
                last_unit = denominator_unit
            elif last_unit:
                denominator_unit = last_unit
                warnings.append(
                    f"Row {row_num}: Empty unit cell, inherited '{last_unit}' from previous row"
                )

        # --- Extract factor values based on layout type ---
        row_has_data = False

        for year_map in schema.years:
            if schema.layout_type == "simple":
                if year_map.value_column is None:
                    continue
                value = cells.get(year_map.value_column)
                factor = _parse_numeric(value)
                if factor is not None:
                    row_has_data = True
                    factors.append(
                        EmissionFactorRecord(
                            year=year_map.year,
                            factor_value=factor,
                            denominator_unit=denominator_unit,
                            source=source,
                            emission_category_name=emission_category_name,
                            parent_category=current_parent,
                        )
                    )

            elif schema.layout_type == "sub_columns":
                # Find the primary sub-column (e.g., "Total")
                total_col: Optional[int] = None
                for sc in year_map.sub_columns or []:
                    if sc.name == year_map.primary_sub_column:
                        total_col = sc.column_index
                        break
                if total_col is None:
                    continue
                value = cells.get(total_col)
                factor = _parse_numeric(value)
                if factor is not None:
                    row_has_data = True
                    factors.append(
                        EmissionFactorRecord(
                            year=year_map.year,
                            factor_value=factor,
                            denominator_unit=denominator_unit,
                            source=source,
                            emission_category_name=emission_category_name,
                            parent_category=current_parent,
                        )
                    )

            elif schema.layout_type == "disposal_pivot":
                # Each disposal column produces a separate record
                for disp_col in year_map.disposal_columns or []:
                    value = cells.get(disp_col.column_index)
                    factor = _parse_numeric(value)
                    if factor is not None:
                        row_has_data = True
                        pivot_name = (
                            f"{emission_category_name}"
                            f"{schema.descriptor_join_separator}"
                            f"{disp_col.name}"
                        )
                        factors.append(
                            EmissionFactorRecord(
                                year=year_map.year,
                                factor_value=factor,
                                denominator_unit=denominator_unit,
                                source=source,
                                emission_category_name=pivot_name,
                                parent_category=current_parent,
                            )
                        )

        if not row_has_data and descriptor_parts:
            # This might be a group-header row — not a warning
            pass

    return factors, warnings


# ---------------------------------------------------------------------------
# Post-processing: disambiguate duplicate names with different units
# ---------------------------------------------------------------------------

def _disambiguate_by_unit(
    factors: list[EmissionFactorRecord],
) -> list[EmissionFactorRecord]:
    """
    When the same emission_category_name appears with different denominator_units
    (e.g. "Road - Van - Diesel" at both tonne.km and km), append a unit-based
    suffix to make them unique.
    """
    from collections import defaultdict

    name_units: dict[str, set[str]] = defaultdict(set)
    for f in factors:
        if f.emission_category_name and f.denominator_unit:
            name_units[f.emission_category_name].add(f.denominator_unit)

    # Only names that appear with 2+ different units need disambiguation
    ambiguous = {name for name, units in name_units.items() if len(units) > 1}
    if not ambiguous:
        return factors

    def _unit_suffix(unit: str) -> str:
        """Extract the meaningful denominator part: 'Kg CO2e/tonne.km' → 'tonne.km'"""
        if "/" in unit:
            return unit.split("/", 1)[1].strip()
        return unit

    result: list[EmissionFactorRecord] = []
    for f in factors:
        if f.emission_category_name in ambiguous and f.denominator_unit:
            suffix = _unit_suffix(f.denominator_unit)
            result.append(
                f.model_copy(
                    update={
                        "emission_category_name": f"{f.emission_category_name} [{suffix}]"
                    }
                )
            )
        else:
            result.append(f)
    return result


# ---------------------------------------------------------------------------
# Public entry point — Emission Factor Excel Parser
# ---------------------------------------------------------------------------

def parse_emission_factor_excel(
    file_path: Path, filename: str
) -> ParseExcelResponse:
    """Main entry: two-pass Excel parsing."""
    wb = openpyxl.load_workbook(str(file_path), data_only=True)
    ws = wb.active

    # Pass 1: Schema detection via LLM
    header_text = _read_header_rows(ws, max_rows=20)
    logger.info(f"Pass 1: Detecting schema for {filename}")
    schema = _detect_schema(header_text)
    logger.info(
        f"Detected layout: {schema.layout_type}, "
        f"years: {[y.year for y in schema.years]}, "
        f"descriptors: {[d.header_name for d in schema.descriptor_columns]}"
    )

    # Pass 2: Deterministic extraction
    logger.info(f"Pass 2: Extracting factors from {filename}")
    factors, warnings = _extract_factors(ws, schema)

    wb.close()

    # Pass 3: Disambiguate duplicate names that differ only by unit
    factors = _disambiguate_by_unit(factors)

    available_years = sorted(set(f.year for f in factors))
    parent_categories = sorted(
        set(f.parent_category for f in factors if f.parent_category)
    )

    logger.info(
        f"Extracted {len(factors)} emission factor records "
        f"across years {available_years} from {filename}"
    )

    return ParseExcelResponse(
        filename=filename,
        factors=factors,
        schema_detected=schema,
        warnings=warnings,
        total_records=len(factors),
        available_years=available_years,
        parent_categories=parent_categories,
    )


# ---------------------------------------------------------------------------
# Category inference via LLM
# ---------------------------------------------------------------------------

CATEGORY_INFERENCE_PROMPT = """\
You are an expert at matching emission factor categories.

You will receive:
1. A list of PARENT CATEGORIES extracted from an Excel emission factor spreadsheet.
2. A list of DATABASE CATEGORIES (each with an id and name) from the user's system.

Your task is to match each parent category to the most appropriate database category.
Consider semantic similarity, not just exact string matching. For example:
- "Stationary Combustion" might match "Scope 1 - Stationary Combustion"
- "Business travel" might match "Business Travel" or "Scope 3 - Business Travel"
- "Downstream transportation and distribustion" (note typo) should still match "Downstream Transportation"

For each parent category, assign a confidence level:
- "high": Very clear match (near-exact or obvious semantic equivalence)
- "medium": Reasonable match but some ambiguity
- "low": No good match found

If no database category is a reasonable match, set suggested_category_id to null.

Return ONLY a JSON object (no extra text):
{
  "suggestions": [
    {
      "parent_category": "<exact parent category string>",
      "suggested_category_id": <int or null>,
      "suggested_category_name": "<name of matched DB category or null>",
      "confidence": "high" | "medium" | "low"
    }
  ]
}
"""


def infer_category_mapping(
    parent_categories: list[str],
    db_categories: list[DbCategory],
) -> list[CategorySuggestion]:
    """Use LLM to match Excel parent categories to DB categories."""
    user_content = (
        f"PARENT CATEGORIES FROM EXCEL:\n"
        f"{parent_categories}\n\n"
        f"DATABASE CATEGORIES:\n"
        f"{[{'id': c.id, 'name': c.name} for c in db_categories]}"
    )

    messages = [
        {"role": "system", "content": CATEGORY_INFERENCE_PROMPT},
        {"role": "user", "content": user_content},
    ]

    logger.info(
        f"Category inference: matching {len(parent_categories)} parent categories "
        f"against {len(db_categories)} DB categories"
    )
    raw_response = _call_openrouter(messages)
    result = _parse_json_object(raw_response)

    suggestions = [
        CategorySuggestion(**item) for item in result.get("suggestions", [])
    ]

    logger.info(
        f"Category inference complete: "
        f"{sum(1 for s in suggestions if s.suggested_category_id is not None)} matched"
    )
    return suggestions


# ===========================================================================
# PART 2: Data Import Excel Parser (Pavithra's bulk upload flow)
# ===========================================================================

# ---------------------------------------------------------------------------
# In-memory file cache — avoids re-downloading from Cloudinary on each step
# TTL = 10 minutes, auto-evicts stale entries
# ---------------------------------------------------------------------------
import time as _time

_file_cache: dict[int, tuple[bytes, str, float]] = {}  # doc_id → (bytes, ext, timestamp)
_FILE_CACHE_TTL = 600  # 10 minutes


def _cache_get(document_id: int) -> tuple[bytes, str] | None:
    entry = _file_cache.get(document_id)
    if entry is None:
        return None
    content, ext, ts = entry
    if _time.monotonic() - ts > _FILE_CACHE_TTL:
        _file_cache.pop(document_id, None)
        return None
    return content, ext


def _cache_set(document_id: int, content: bytes, ext: str) -> None:
    # Evict stale entries (keep cache bounded)
    now = _time.monotonic()
    stale = [k for k, (_, _, ts) in _file_cache.items() if now - ts > _FILE_CACHE_TTL]
    for k in stale:
        _file_cache.pop(k, None)
    _file_cache[document_id] = (content, ext, now)


UNIT_CONVERSIONS = {
    "litre": {"gallon": 0.264172, "ml": 1000, "cubic meter": 0.001, "kilo litre": 0.001, "kl": 0.001},
    "kl": {"litre": 1000, "gallon": 264.172, "ml": 1000000, "cubic meter": 1},
    "kilo litre": {"litre": 1000, "gallon": 264.172, "ml": 1000000, "cubic meter": 1},
    "gallon": {"litre": 3.78541, "ml": 3785.41, "cubic meter": 0.00378541, "kl": 0.00378541},
    "ml": {"litre": 0.001, "gallon": 0.000264172, "kl": 0.000001},
    "cubic meter": {"litre": 1000, "gallon": 264.172, "kl": 1},

    # Weight
    "kg": {"lb": 2.20462, "tonne": 0.001, "g": 1000, "ton": 0.00110231},
    "lb": {"kg": 0.453592, "tonne": 0.000453592, "g": 453.592},
    "ton": {"kg": 907.185, "lb": 2000, "g": 907185},
    "tonne": {"kg": 1000, "lb": 2204.62, "g": 1000000},
    "g": {"kg": 0.001, "lb": 0.00220462},

    # Energy
    "kwh": {"mwh": 0.001, "gj": 0.0036, "mj": 3.6},
    "mwh": {"kwh": 1000, "gj": 3.6, "mj": 3600},
    "gj": {"kwh": 277.778, "mwh": 0.277778, "mj": 1000},
    "mj": {"kwh": 0.277778, "gj": 0.001},

    # Currency
    "inr": {"usd": 0.012},
    "usd": {"inr": 83.5, "eur": 0.92},
    "eur": {"usd": 1.09},
}

def get_conversion_factor(from_unit: str, to_unit: str):
    f = (from_unit or "").lower().strip()
    t = (to_unit or "").lower().strip()
    return UNIT_CONVERSIONS.get(f, {}).get(t)

def units_match_exact(u1: str | None, u2: str | None) -> bool:
    if not u1 or not u2:
        return True
    return u1.lower().strip() == u2.lower().strip()

# ---------- Excel reading ----------
def _read_df_from_bytes(content: bytes, ext: str) -> pd.DataFrame:
    for header_row in [0, 1, 2]:
        try:
            if ext == "csv":
                df = pd.read_csv(io.BytesIO(content), dtype=str, header=header_row)
            else:
                df = pd.read_excel(io.BytesIO(content), dtype=str, header=header_row)

            valid_cols = [c for c in df.columns if not str(c).startswith("Unnamed:")]
            if valid_cols:
                df = df.fillna("")
                return df
        except Exception:
            continue
    raise ValueError("Failed to parse file: no valid headers found.")

def map_df(df: pd.DataFrame, mappings: dict[str, str]) -> pd.DataFrame:
    mapped = {}
    for required_field, excel_col in mappings.items():
        col = str(excel_col).strip()
        if col in df.columns:
            mapped[required_field] = df[col]
        else:
            logger.warning(f"Column '{col}' not found in file, skipping.")
    if not mapped:
        raise ValueError("No valid column mappings found.")
    return pd.DataFrame(mapped).fillna("")

def download_document_bytes(document_id: int) -> tuple[bytes, str]:
    # Check in-memory cache first (avoids re-reading on each step)
    cached = _cache_get(document_id)
    if cached is not None:
        return cached

    doc = get_uploaded_document_by_id(document_id)
    if not doc:
        raise ValueError("Invalid document_id")

    file_path = doc["cloudinary_url"]  # now stores local path

    # local file path
    if os.path.isfile(file_path):
        with open(file_path, "rb") as f:
            content = f.read()
        ext = file_path.rsplit(".", 1)[-1].lower()
        if ext not in {"csv", "xls", "xlsx"}:
            ext = "xlsx"
        _cache_set(document_id, content, ext)
        return content, ext

    # If the path looks like a local filesystem path (no URL scheme),
    # the temp file was deleted — don't attempt requests.get() on it.
    if not file_path.startswith(("http://", "https://")):
        raise ValueError(
            f"Temp file no longer exists at '{file_path}'. Please re-upload the file."
        )

    # fallback: legacy Cloudinary URL
    try:
        r = requests.get(file_path, timeout=60)
        r.raise_for_status()
    except Exception as e:
        raise ValueError(f"Failed to read file: {e}")

    filename = file_path.split("?")[0].lower()
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in {"csv", "xls", "xlsx"}:
        ext = "xlsx"

    _cache_set(document_id, r.content, ext)
    return r.content, ext

def _build_category_resolver(site_id: int, category_id: int) -> dict[str, str]:
    """
    Build a case-insensitive lookup from display category names (as they appear
    in uploaded files) to emission_category_name (as stored in emission_factors).

    Resolution order:
      1. column_config.emission_category_mapping JSONB (pipe-separated keys)
      2. Fallback: emission_category_mapping table (ECM) which stores
         company_category_name → global_category_name per company/site/category.

    Returns empty dict if no mapping source has data.
    """
    resolver: dict[str, str] = {}

    # 1. Try column_config's emission_category_mapping JSONB
    try:
        config = fetch_column_config(site_id, category_id)
        if config:
            ecm = config.get("emission_category_mapping") or {}
            for display_key, ef_name in ecm.items():
                if display_key and ef_name:
                    resolver[str(display_key).strip().lower()] = str(ef_name).strip()
    except Exception as e:
        logger.warning(f"Failed to load column_config for category resolver: {e}")

    if resolver:
        return resolver

    # 2. Fallback: query emission_category_mapping table (ECM)
    conn = get_connection()
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT company_category_name, global_category_name
                FROM emission_category_mapping
                WHERE category_id = %s
                  AND (site_id = %s OR site_id IS NULL)
                ORDER BY site_id DESC NULLS LAST
                """,
                (category_id, site_id),
            )
            for row in cur.fetchall():
                key = (row["company_category_name"] or "").strip().lower()
                val = (row["global_category_name"] or "").strip()
                if key and val and key not in resolver:
                    resolver[key] = val
        if resolver:
            logger.info(
                f"Category resolver built from ECM table: "
                f"site_id={site_id}, category_id={category_id}, "
                f"{len(resolver)} mappings"
            )
    except Exception as e:
        logger.warning(f"Failed to load ECM fallback for category resolver: {e}")
    finally:
        release_connection(conn)

    return resolver


# ---------- Step 2: unique categories ----------
def get_unique_categories(document_id: int, mappings: dict[str, str]) -> tuple[list[str], int]:
    content, ext = download_document_bytes(document_id)
    df = _read_df_from_bytes(content, ext)
    mapped_df = map_df(df, mappings)

    if "emission_category" not in mapped_df.columns:
        raise ValueError("Mapping must include emission_category")

    cats = sorted(
        {str(x).strip() for x in mapped_df["emission_category"].tolist() if str(x).strip()}
    )
    return cats, len(mapped_df)


def get_preview_rows(
    document_id: int,
    mappings: dict[str, str],
    selected_categories: list[str],
    page: int,
    page_size: int,
    site_id: int,
    category_id: int,
    date_of_reporting: str,
) -> tuple[list[dict], int]:
    content, ext = download_document_bytes(document_id)
    df = _read_df_from_bytes(content, ext)
    mapped_df = map_df(df, mappings)

    if selected_categories:
        selected = {c.strip() for c in selected_categories}
        if "emission_category" in mapped_df.columns:
            mapped_df = mapped_df[
                mapped_df["emission_category"].astype(str).str.strip().isin(selected)
            ]

    total = len(mapped_df)  # count AFTER category filter for correct pagination

    start = max(0, (page - 1) * page_size)
    end = start + page_size
    page_df = mapped_df.iloc[start:end]

    # Resolve uploaded category names → EF names via column_config mapping
    category_resolver = _build_category_resolver(site_id, category_id)

    conn = get_connection()
    try:
        out: list[dict] = []
        year = int(date_of_reporting[:4]) - 1

        # Batch-load all emission factors for this (site, category)
        # instead of querying per row (N+1 → 1 query).
        # Load ALL years and prefer exact year match, fallback to latest.
        ef_map: dict[str, tuple[float, str | None]] = {}
        try:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT emission_category_name, factor_value, denominator_unit, year
                    FROM emission_factors
                    WHERE site_id = %s AND category_id = %s
                    ORDER BY year DESC
                    """,
                    (site_id, category_id),
                )
                # Group by name, prefer exact year match, fallback to latest year
                all_factors: dict[str, list[dict]] = {}
                for row in cur.fetchall():
                    name = (row["emission_category_name"] or "").strip().lower()
                    if name:
                        all_factors.setdefault(name, []).append(row)
                for name, rows in all_factors.items():
                    exact = [r for r in rows if r["year"] == year]
                    best = exact[0] if exact else rows[0]  # rows already sorted DESC
                    ef_map[name] = (float(best["factor_value"]), best.get("denominator_unit"))
        except Exception as e:
            logger.warning(f"Failed to preload emission factors for preview: {e}")

        logger.info(
            f"Preview EF lookup: site_id={site_id}, category_id={category_id}, "
            f"year={year}, ef_map_keys={list(ef_map.keys())}, "
            f"category_resolver_keys={list(category_resolver.keys())}"
        )

        for _, r in page_df.iterrows():
            activity_data = r.to_dict()
            activity_unit = str(activity_data.get("activity_data_unit") or "").strip() or None
            emission_category = str(activity_data.get("emission_category") or "").strip()

            factor_value = None
            denominator_unit = None
            total_emission = 0.0
            global_category_name = None

            if emission_category:
                display_key = emission_category.strip().lower()
                # Resolve display name → EF name via column_config mapping,
                # fall back to direct name match if no mapping exists
                resolved = category_resolver.get(display_key)
                ef_key = resolved.strip().lower() if resolved else display_key
                ef_entry = ef_map.get(ef_key)
                if not ef_entry:
                    logger.warning(
                        f"No emission factor found for category '{emission_category}' "
                        f"(ef_key='{ef_key}', resolved='{resolved}'). "
                        f"Available keys: {list(ef_map.keys())}"
                    )
                if ef_entry:
                    global_category_name = resolved if resolved else emission_category
                    factor_value, denominator_unit = ef_entry
                    activity_value = _extract_activity_value(activity_data)
                    if activity_value > 0:
                        if units_match_exact(denominator_unit, activity_unit):
                            total_emission = round((activity_value * factor_value) / 1000.0, 2)
                        else:
                            conv = get_conversion_factor(activity_unit or "", denominator_unit or "")
                            if conv:
                                total_emission = round((activity_value * float(conv) * factor_value) / 1000.0, 2)
                            else:
                                logger.warning(
                                    f"No unit conversion for '{activity_unit}' → '{denominator_unit}' "
                                    f"(category='{emission_category}')"
                                )

            row_out = dict(activity_data)
            row_out["global_category_name"] = global_category_name
            row_out["factor_value"] = factor_value
            row_out["denominator_unit"] = denominator_unit
            row_out["total_emission"] = total_emission
            row_out["unit"] = "tCO2e"

            out.append(row_out)

        return out, total
    finally:
        release_connection(conn)

def _extract_activity_value(activity_data: dict) -> float:
    """
    Extract a numeric activity value from a mapped row dict.
    Mirrors your Node/JS logic:
      1) try common field names
      2) else first numeric >= 100
      3) else first numeric > 0
    """
    skip_cols = {
        "material",
        "disposal_method",
        "fuel_type",
        "vehicle_type",
        "source_type",
        "waste_type",
        "transport_mode",
    }

    common_fields = [
        "activity_value",
        "quantity",
        "value",
        "amount",
        "consumption",
        "activity data",
        "activity_data",
    ]

    keys = list(activity_data.keys())

    # 1) exact common fields first
    for field in common_fields:
        match = next((k for k in keys if str(k).lower().strip() == field.lower().strip()), None)
        if match:
            v = activity_data.get(match)
            if v not in (None, ""):
                try:
                    num = float(str(v).replace(",", "").strip())
                    if num > 0:
                        return num
                except Exception:
                    pass

    # 2) numeric >= 100
    for k, v in activity_data.items():
        if str(k).lower().strip() in skip_cols:
            continue
        if str(k).lower().strip() == "emission_category":
            continue
        if v in (None, ""):
            continue
        try:
            num = float(str(v).replace(",", "").strip())
            if num >= 100:
                return num
        except Exception:
            continue

    # 3) any positive numeric
    for k, v in activity_data.items():
        if str(k).lower().strip() in skip_cols:
            continue
        if str(k).lower().strip() == "emission_category":
            continue
        if v in (None, ""):
            continue
        try:
            num = float(str(v).replace(",", "").strip())
            if num > 0:
                return num
        except Exception:
            continue

    return 0.0


def _calc_emission(conn, site_id: int, category_id: int, activity_data: dict, activity_unit: str | None, date_of_reporting: str) -> float:
    emission_category = (activity_data.get("emission_category") or "").strip()
    if not emission_category:
        return 0.0

    # reportingYear - 1 (same as Node)
    year = int(date_of_reporting[:4]) - 1

    ef = fetch_emission_factor(conn, site_id, category_id, year, emission_category)
    if not ef:
        return 0.0

    factor_value = float(ef["factor_value"])
    denom_unit = ef.get("denominator_unit")

    activity_value = _extract_activity_value(activity_data)
    if activity_value <= 0:
        return 0.0

    if units_match_exact(denom_unit, activity_unit):
        return round((activity_value * factor_value) / 1000.0, 2)

    conv = get_conversion_factor(activity_unit or "", denom_unit or "")
    if not conv:
        return 0.0

    converted = activity_value * float(conv)
    return round((converted * factor_value) / 1000.0, 2)


def import_all_rows(
    document_id: int,
    mappings: dict[str, str],
    selected_categories: list[str],
    site_id: int,
    category_id: int,
    date_of_reporting: str,
    chunk_size: int = 2000,
) -> dict:
    import uuid
    upload_batch_id = str(uuid.uuid4())

    content, ext = download_document_bytes(document_id)
    df = _read_df_from_bytes(content, ext)
    mapped_df = map_df(df, mappings)

    total_rows = len(mapped_df)

    if selected_categories:
        selected = {c.strip() for c in selected_categories}
        if "emission_category" in mapped_df.columns:
            mapped_df = mapped_df[
                mapped_df["emission_category"].astype(str).str.strip().isin(selected)
            ]

    # Resolve uploaded category names → EF names via column_config mapping
    category_resolver = _build_category_resolver(site_id, category_id)

    conn = get_connection()
    inserted = 0
    skipped = 0

    year = int(date_of_reporting[:4]) - 1
    ef_map: dict[str, dict] = {}
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT emission_factor_id, emission_category_name, global_category_name,
                       factor_value, denominator_unit, source, year
                FROM emission_factors
                WHERE site_id = %s
                  AND category_id = %s
                ORDER BY year DESC
                """,
                (site_id, category_id),
            )
            # Group by name, prefer exact year match, fallback to latest year
            all_factors: dict[str, list[dict]] = {}
            for row in cur.fetchall():
                name = (row["emission_category_name"] or "").strip().lower()
                if not name:
                    continue
                all_factors.setdefault(name, []).append(row)
            for name, rows in all_factors.items():
                exact = [r for r in rows if r["year"] == year]
                best = exact[0] if exact else rows[0]  # rows already sorted DESC
                ef_map[name] = best
    except Exception as e:
        logger.warning(f"Failed to preload emission factors for import: {e}")
        ef_map = {}

    # FERA (Fuel and Energy Related Activities) - preload FERA emission factors
    FERA_CATEGORY_ID = 28
    FERA_TRIGGER_CATEGORIES = {1, 2, 4}  # Stationary Combustion, Mobile Combustion, Purchased Electricity
    fera_ef_map: dict[str, dict] = {}
    if category_id in FERA_TRIGGER_CATEGORIES:
        try:
            from psycopg2.extras import RealDictCursor
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT emission_factor_id, emission_category_name, global_category_name,
                           factor_value, denominator_unit, source, year
                    FROM emission_factors
                    WHERE site_id = %s
                      AND category_id = %s
                    ORDER BY year DESC
                    """,
                    (site_id, FERA_CATEGORY_ID),
                )
                fera_all_factors: dict[str, list[dict]] = {}
                for row in cur.fetchall():
                    name = (row["emission_category_name"] or "").strip().lower()
                    if not name:
                        continue
                    fera_all_factors.setdefault(name, []).append(row)
                for name, rows in fera_all_factors.items():
                    exact = [r for r in rows if r["year"] == year]
                    best = exact[0] if exact else rows[0]
                    fera_ef_map[name] = best
        except Exception as e:
            logger.warning(f"Failed to preload FERA emission factors: {e}")

    try:
        rows_buffer: list[dict] = []
        fera_rows_buffer: list[dict] = []

        for _, row in mapped_df.iterrows():
            activity_data = row.to_dict()

            # Separate extra_ prefixed fields into extra_data
            extra_data: dict = {}
            for k in list(activity_data.keys()):
                if k.startswith("extra_"):
                    val = activity_data.pop(k)
                    real_key = k[len("extra_"):]
                    if val is not None and str(val).strip():
                        extra_data[real_key] = str(val).strip()

            activity_unit = str(activity_data.get("activity_data_unit") or "").strip() or None
            emission_category = str(activity_data.get("emission_category") or "").strip()

            matched_ef: dict | None = None
            if ef_map and emission_category:
                display_key = emission_category.strip().lower()
                resolved = category_resolver.get(display_key)
                ef_key = resolved.strip().lower() if resolved else display_key
                if ef_key in ef_map:
                    matched_ef = ef_map[ef_key]
                    factor_value = float(matched_ef.get("factor_value") or 0)
                    denom_unit = matched_ef.get("denominator_unit")
                    activity_value = _extract_activity_value(activity_data)
                    if activity_value <= 0:
                        total_emission = 0.0
                    elif units_match_exact(denom_unit, activity_unit):
                        total_emission = round((activity_value * factor_value) / 1000.0, 2)
                    else:
                        conv = get_conversion_factor(activity_unit or "", denom_unit or "")
                        if not conv:
                            total_emission = 0.0
                        else:
                            total_emission = round((activity_value * float(conv) * factor_value) / 1000.0, 2)
                else:
                    total_emission = 0.0
            else:
                total_emission = _calc_emission(conn, site_id, category_id, activity_data, activity_unit, date_of_reporting)

            ef_snapshot = None
            if matched_ef:
                ef_snapshot = {
                    "emission_factor_id": matched_ef.get("emission_factor_id"),
                    "emission_category_name": matched_ef.get("emission_category_name"),
                    "global_category_name": matched_ef.get("global_category_name"),
                    "factor_value": float(matched_ef.get("factor_value") or 0),
                    "denominator_unit": matched_ef.get("denominator_unit"),
                    "source": matched_ef.get("source"),
                    "year": matched_ef.get("year"),
                }

            rows_buffer.append(
                {
                    "site_id": site_id,
                    "category_id": category_id,
                    "activity_data": activity_data,
                    "extra_data": extra_data,
                    "total_emission": total_emission,
                    "unit": "tCO2e",
                    "date_of_reporting": date_of_reporting,
                    "activity_data_unit": activity_unit,
                    "upload_batch_id": upload_batch_id,
                    "emission_factor_snapshot": ef_snapshot,
                }
            )

            # Auto-create FERA emission row if applicable
            if fera_ef_map and emission_category:
                fera_key = emission_category.strip().lower()
                resolved_fera = category_resolver.get(fera_key)
                fera_lookup = resolved_fera.strip().lower() if resolved_fera else fera_key
                if fera_lookup in fera_ef_map:
                    fera_matched = fera_ef_map[fera_lookup]
                    fera_factor_value = float(fera_matched.get("factor_value") or 0)
                    fera_denom_unit = fera_matched.get("denominator_unit")
                    fera_activity_value = _extract_activity_value(activity_data)
                    if fera_activity_value > 0:
                        if units_match_exact(fera_denom_unit, activity_unit):
                            fera_emission = round((fera_activity_value * fera_factor_value) / 1000.0, 2)
                        else:
                            fera_conv = get_conversion_factor(activity_unit or "", fera_denom_unit or "")
                            fera_emission = round((fera_activity_value * float(fera_conv) * fera_factor_value) / 1000.0, 2) if fera_conv else 0.0
                        if fera_emission > 0:
                            fera_activity = dict(activity_data)
                            fera_activity["_source_category_id"] = category_id
                            fera_activity["_auto_fera"] = True
                            fera_ef_snapshot = {
                                "emission_factor_id": fera_matched.get("emission_factor_id"),
                                "emission_category_name": fera_matched.get("emission_category_name"),
                                "global_category_name": fera_matched.get("global_category_name"),
                                "factor_value": fera_factor_value,
                                "denominator_unit": fera_denom_unit,
                                "source": fera_matched.get("source"),
                                "year": fera_matched.get("year"),
                            }
                            fera_rows_buffer.append(
                                {
                                    "site_id": site_id,
                                    "category_id": FERA_CATEGORY_ID,
                                    "activity_data": fera_activity,
                                    "extra_data": extra_data,
                                    "total_emission": fera_emission,
                                    "unit": "tCO2e",
                                    "date_of_reporting": date_of_reporting,
                                    "activity_data_unit": activity_unit,
                                    "upload_batch_id": upload_batch_id,
                                    "emission_factor_snapshot": fera_ef_snapshot,
                                }
                            )

            if len(rows_buffer) >= chunk_size:
                inserted += bulk_insert_emissions_with_conn(conn, rows_buffer)
                rows_buffer = []
            if len(fera_rows_buffer) >= chunk_size:
                inserted += bulk_insert_emissions_with_conn(conn, fera_rows_buffer)
                fera_rows_buffer = []

        if rows_buffer:
            inserted += bulk_insert_emissions_with_conn(conn, rows_buffer)
        if fera_rows_buffer:
            inserted += bulk_insert_emissions_with_conn(conn, fera_rows_buffer)

        conn.commit()
        return {"inserted": inserted, "skipped": skipped, "total_rows": total_rows, "upload_batch_id": upload_batch_id if inserted > 0 else None}

    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)
