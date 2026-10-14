"""XI1 · Workbook reader (docs/excel-import/modules/XI1-workbook-reader).

``read_workbook(path)`` turns any .xlsx/.xls/.csv into a deterministic,
model-free description: every sheet, its header row (and depth, for merged
multi-row headers), cleaned data rows with merged cells forward-filled and
subtotal/total/blank rows dropped, per-column statistics, a layout guess
("rows" or "months_as_columns") and a fingerprint that is stable across sheet
renames, row counts and column order. Messy layouts stop being the model's
problem: the mapping engine (XI2) and the model only ever see this description.

Nothing here calls ESG-lite or a model. Cell text is data: it is carried as
written and never interpreted. Nothing from a sheet is logged.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import math
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.mapping import MAPPING_SHEETS_PER_WORKBOOK

# How many rows at the top of a sheet are searched for the header row.
HEADER_SCAN_ROWS = 20
# A header row has at least this share of text among its non-empty cells.
HEADER_TEXT_RATIO = 0.6
# A multi-row header is at most this deep.
HEADER_MAX_DEPTH = 3
# A sheet with fewer data rows than this is listed with a skipped_reason.
MIN_DATA_ROWS = 2
# Distinct sample values kept per column.
SAMPLE_VALUES = 5
# Months-as-columns: at least this many header cells parse as months.
MONTH_COLUMNS_MIN = 6
# Cells wider than this are cut in the description (never in the rows).
HEADER_MAX_CHARS = 200
# Columns read per sheet: a sheet formatted out to column XFD is read only as wide as its last value, and never wider than this.
MAX_COLUMNS = 512

DType = str  # "text" | "number" | "date" | "mixed" | "empty"

# A total label: the word alone, or followed by punctuation, a year or number, a bracket, or "for"/"of" ("Total (kWh)",
# "Sub-total: Plant A", "Total for FY25"). "Total Energies" (an activity or supplier) is not one.
_TOTAL_RE = re.compile(
    r"^\s*(grand\s+)?(sub[\s-]?)?totals?\s*($|[:\-–(\[]|\d|\w*\d|(for|of|ytd|fy|cy|year|annual|quarter|month)\b)", re.IGNORECASE
)
# "Q1 subtotal", "Plant A Total", "Site sub-total:": the label ends with the word.
_TOTAL_END_RE = re.compile(r"(^|[\s(\[\-–])(sub[\s-]?)?totals?\s*[:)\]]?\s*$", re.IGNORECASE)
# Whole numbers read as year headings ("Supplier | 2023 | 2024").
_YEAR_MIN, _YEAR_MAX = 1990, 2100
_DAY_FIRST_RE = re.compile(r"^(?P<a>\d{1,2})[/.-](?P<b>\d{1,2})[/.-](?P<y>\d{4})$|^(?P<y2>\d{4})[/.](?P<m2>\d{1,2})[/.](?P<d2>\d{1,2})$")
_LETTER_LABEL_RE = re.compile(r"^[A-Z]{1,2}$")
_NUMBER_RE = re.compile(r"^[+-]?\s*(\d{1,3}(,\d{2,3})+|\d+)(\.\d+)?\s*$")
_DATE_TEXT_RE = re.compile(
    r"^(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}|[A-Za-z]{3,9}[-\s]\d{1,2},?\s\d{4})$"
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAME_RE = re.compile(
    r"^(?P<m>[A-Za-z]{3,9})\.?[\s'\-/]*(?P<y>\d{2}|\d{4})?$|^(?P<y2>\d{4})[\s\-/]+(?P<m2>[A-Za-z]{3,9})\.?$"
)
_MONTH_ISO_RE = re.compile(r"^(?P<y>\d{4})[-/](?P<m>\d{1,2})$|^(?P<m2>\d{1,2})[-/](?P<y2>\d{4})$")

# Unit tokens recognised in headers ("Qty (L)", "Consumption kWh") and cells ("1,200 kWh").
_UNIT_TOKENS = [
    "kwh", "mwh", "gwh", "kcal", "mmbtu", "gj", "mj", "tj", "therm",
    "litre", "litres", "liter", "liters", "ltr", "ltrs", "lt", "l", "kl", "ml", "m3", "m³", "scm", "nm3", "mmscm", "gallon", "gallons", "gal",
    "kg", "kgs", "g", "mt", "t", "tonne", "tonnes", "ton", "tons", "lb", "lbs",
    "km", "miles", "mile", "m", "nm",
    "inr", "usd", "eur", "gbp", "aed", "rs", "₹", "$", "€", "£",
    "nos", "no", "pcs", "units", "hrs", "hours", "hr", "days", "tkm", "pkm", "pax",
]
_UNIT_SET = set(_UNIT_TOKENS)
_HEADER_UNIT_RE = re.compile(r"\(([^()]{1,20})\)|\[([^\[\]]{1,20})\]|\bin\s+([A-Za-z³₹$€£/]{1,12})$|/\s*([A-Za-z³]{1,8})$", re.IGNORECASE)
_CELL_UNIT_RE = re.compile(r"^[+-]?[\d,]*\.?\d+\s*([A-Za-z³₹$€£]{1,8})$")


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------


@dataclass
class Column:
    """One header cell and what sits under it."""

    name: str
    letter: str
    dtype: DType
    distinct: int
    samples: list[str]
    fill_ratio: float
    numeric_ratio: float
    unit_tokens: list[str]
    month: Optional[dict] = None  # {"month": 1..12, "year": int|None, "date": "YYYY-MM-DD"|None} when the header is a month


@dataclass
class DroppedRow:
    row: int  # 1-based sheet row number
    reason: str  # "blank" | "total" | "legend"


@dataclass
class Sheet:
    name: str
    index: int
    header_row: int  # 1-based; the first row of the header
    header_depth: int
    columns: list[Column]
    rows: list[dict]  # kept data rows: {"row": int, "row_ref": "Sheet!A12", "cells": {column name: text}}
    dropped_rows: list[DroppedRow]
    layout: str  # "rows" | "months_as_columns"
    fingerprint: str
    data_rows: int
    skipped_reason: Optional[str] = None
    hidden: bool = False

    @property
    def row_refs(self) -> list[str]:
        return [r["row_ref"] for r in self.rows]

    def sample_rows(self, n: int) -> list[dict]:
        """The first ``n`` kept rows' cells, for a description or a prompt."""
        return [r["cells"] for r in self.rows[:n]]

    def describe(self) -> dict:
        """The sheet without its rows (golden JSON, telemetry, prompts)."""
        d = asdict(self)
        d.pop("rows")
        d["row_refs_head"] = self.row_refs[:3]
        return d


@dataclass
class Workbook:
    name: str
    ext: str
    sheets: list[Sheet] = field(default_factory=list)

    @property
    def readable_sheets(self) -> list[Sheet]:
        return [s for s in self.sheets if s.skipped_reason is None]

    def describe(self) -> dict:
        return {"name": self.name, "ext": self.ext, "sheets": [s.describe() for s in self.sheets]}


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def cell_text(value: Any) -> str:
    """A cell as the text the user sees: whole floats without ".0", dates as ISO, else str()."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if (value.hour, value.minute, value.second) == (0, 0, 0) else value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    return re.sub(r"[ \t\r\f\v]+", " ", str(value)).strip()


def cell_kind(value: Any) -> str:
    """"empty" | "number" | "date" | "text" for one raw cell."""
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "text"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return "empty"
        return "number"
    if isinstance(value, (datetime, date)):
        return "date"
    text = str(value).strip()
    if not text:
        return "empty"
    if _NUMBER_RE.match(text):
        return "number"
    if _DATE_TEXT_RE.match(text):
        return "date"
    return "text"


def normalise_header(name: str) -> str:
    """Lower case, punctuation removed, whitespace collapsed (01-contracts §1)."""
    text = re.sub(r"[^\w\s]", " ", str(name).lower())
    return re.sub(r"\s+", " ", text).strip()


def fingerprint_of(header_names: Iterable[str], header_depth: int) -> str:
    """sha256 of the sorted normalised header names plus the header depth.

    Sorting makes the same headers in another order the same fingerprint; the
    sheet name and the row count play no part.
    """
    names = sorted({normalise_header(n) for n in header_names if normalise_header(n)})
    payload = "\n".join(names) + f"\n#depth={int(header_depth)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def month_of_header(name: str) -> Optional[dict]:
    """``{"month", "year", "date"}`` when a header cell names a month ("Jan", "Jan-24",
    "January 2025", "2025-01", "01/2025", a date cell), else None."""
    text = cell_text(name)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        y, m, _ = (int(p) for p in text.split("-"))
        return _month(m, y)
    m = _DAY_FIRST_RE.match(text)
    if m:
        # Day first ("01/03/2025" is 1 March), as everywhere else in the import; "2025/03/01" is year first.
        if m.group("y2"):
            return _month(int(m.group("m2")), int(m.group("y2")))
        a, b = int(m.group("a")), int(m.group("b"))
        month = b if b <= 12 else a
        return _month(month, int(m.group("y")))
    m = _MONTH_ISO_RE.match(text)
    if m:
        year = int(m.group("y") or m.group("y2"))
        month = int(m.group("m") or m.group("m2"))
        return _month(month, year) if 1 <= month <= 12 else None
    m = _MONTH_NAME_RE.match(text.strip().rstrip("."))
    if not m:
        return None
    word = (m.group("m") or m.group("m2") or "").lower()
    if word not in _MONTHS:
        return None
    year_text = m.group("y") or m.group("y2")
    year = None
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    return _month(_MONTHS[word], year)


def _month(month: int, year: Optional[int]) -> dict:
    if not 1 <= month <= 12:
        return None  # type: ignore[return-value]
    out = {"month": month, "year": year, "date": None}
    if year is not None and 1990 <= year <= 2100:
        out["date"] = _month_end(year, month)
    elif year is not None:
        return None  # type: ignore[return-value]
    return out


def _month_end(year: int, month: int) -> str:
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def unit_tokens_in_header(name: str) -> list[str]:
    out: list[str] = []
    bare = str(name).strip().lower()
    if bare in _UNIT_SET:
        out.append(bare)
    for m in _HEADER_UNIT_RE.finditer(str(name)):
        token = next((g for g in m.groups() if g), "")
        token = token.strip().lower()
        if token in _UNIT_SET and token not in out:
            out.append(token)
    return out


def unit_token_in_cell(text: str) -> Optional[str]:
    m = _CELL_UNIT_RE.match(text.strip())
    if not m:
        return None
    token = m.group(1).lower()
    return token if token in _UNIT_SET else None


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------


@dataclass
class _Grid:
    """A sheet as raw rows of python values, 1-based row numbers kept."""

    name: str
    index: int
    rows: list[list[Any]]  # rows[i] is sheet row i+1
    hidden: bool = False
    first_col: int = 1  # 1-based column of rows[i][0]
    chart: bool = False  # a chart sheet: listed, never read


def _xlsx_grids(content: bytes) -> list[_Grid]:
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError, ValueError) as e:
        raise ValueError("This file could not be read as an .xlsx workbook. Open it in Excel, save it again and upload it.") from e
    grids: list[_Grid] = []
    try:
        # wb.sheetnames lists chart sheets too, so every sheet keeps its place in the workbook.
        for index, title in enumerate(wb.sheetnames):
            ws = wb[title]
            if not isinstance(ws, Worksheet):
                grids.append(_Grid(name=str(title), index=index, rows=[], hidden=getattr(ws, "sheet_state", "visible") != "visible", chart=True))
                continue
            # Only cells holding a value count: a format applied out to XFD1048576 must not size the grid.
            values = {(r, c): cell.value for (r, c), cell in ws._cells.items() if cell.value is not None}
            max_row = max((r for r, _ in values), default=0)
            max_col = min(max((c for _, c in values), default=0), MAX_COLUMNS)
            rows: list[list[Any]] = [[None] * max_col for _ in range(max_row)]
            for (r, c), v in values.items():
                if c <= max_col:
                    rows[r - 1][c - 1] = v
            # Merged cells: every cell of a range reads as the range's top-left value.
            for rng in list(ws.merged_cells.ranges):
                top = rows[rng.min_row - 1][rng.min_col - 1] if rng.min_row - 1 < max_row and rng.min_col - 1 < max_col else None
                if top is None:
                    continue
                for r in range(rng.min_row, min(rng.max_row, max_row) + 1):
                    for c in range(rng.min_col, min(rng.max_col, max_col) + 1):
                        if rows[r - 1][c - 1] is None:
                            rows[r - 1][c - 1] = top
            grids.append(_Grid(name=str(ws.title), index=index, rows=rows, hidden=ws.sheet_state != "visible"))
    finally:
        wb.close()
    return grids


def _csv_grids(content: bytes, name: str) -> list[_Grid]:
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[(v if v.strip() else None) for v in row] for row in csv.reader(io.StringIO(text), dialect)]
    width = min(max((len(r) for r in rows), default=0), MAX_COLUMNS)
    rows = [(r + [None] * (width - len(r)))[:width] for r in rows]
    return [_Grid(name=name, index=0, rows=rows)]


def _xls_grids(content: bytes) -> list[_Grid]:
    import pandas as pd

    try:
        frames = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=object)
    except ImportError as e:  # xlrd missing
        raise ValueError("Reading .xls needs the xlrd package; save the file as .xlsx and upload again.") from e
    grids = []
    for index, (name, df) in enumerate(frames.items()):
        rows = [[(None if (isinstance(v, float) and math.isnan(v)) else v) for v in rec[:MAX_COLUMNS]] for rec in df.itertuples(index=False, name=None)]
        grids.append(_Grid(name=str(name), index=index, rows=rows))
    return grids


# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------


def _is_year(value: Any) -> bool:
    text = cell_text(value)
    return bool(re.fullmatch(r"\d{4}", text)) and _YEAR_MIN <= int(text) <= _YEAR_MAX


def _profile(row: list[Any]) -> dict:
    """Share of text, number and date cells among the filled ones, and ``header``:
    the share that can be header cells. Next to at least one text cell, two or
    more year numbers ("2023 | 2024") or six or more month dates count as header
    cells and not as numbers or dates."""
    kinds = [cell_kind(v) for v in row]
    filled = [(k, v) for k, v in zip(kinds, row) if k != "empty"]
    n = len(filled)
    text = sum(k == "text" for k, _ in filled)
    numbers = sum(k == "number" for k, _ in filled)
    dates = sum(k == "date" for k, _ in filled)
    years = sum(k == "number" and _is_year(v) for k, v in filled)
    month_dates = sum(k == "date" and month_of_header(v) is not None for k, v in filled)
    heading_years = years if text and years >= 2 else 0
    heading_dates = month_dates if text and month_dates >= MONTH_COLUMNS_MIN else 0
    return {
        "filled": n,
        "distinct": len({cell_text(v) for _, v in filled}),
        "text": text / n if n else 0.0,
        "header": (text + heading_years + heading_dates) / n if n else 0.0,
        "number": (numbers - heading_years) / n if n else 0.0,
        "date": (dates - heading_dates) / n if n else 0.0,
    }


def _headerish(p: dict) -> bool:
    return p["filled"] >= 2 and p["header"] >= HEADER_TEXT_RATIO and p["number"] == 0 and p["text"] > 0


def _is_title(p: dict) -> bool:
    """One text repeated across the row: a merged title, not a header."""
    return p["filled"] >= 2 and p["distinct"] == 1


def _is_legend(row: list[Any]) -> bool:
    vals = [cell_text(v) for v in row if cell_text(v)]
    return len(vals) >= 3 and all(_LETTER_LABEL_RE.match(v) for v in vals)


def _is_total(row: list[Any]) -> bool:
    """A total row: one of the text cells before the first number is a total
    label. Checking every leading label, not only the first cell, catches a
    "Subtotal" under a merged (filled-down) site name."""
    for v in row:
        kind = cell_kind(v)
        if kind == "number":
            return False
        if kind == "text" and (_TOTAL_RE.match(cell_text(v)) or _TOTAL_END_RE.search(cell_text(v))):
            return True
    return False


def detect_header(rows: list[list[Any]]) -> tuple[int, int]:
    """``(header_row, depth)``, 1-based. The header is the first row in the top
    HEADER_SCAN_ROWS where ≥ 60 % of the non-empty cells are text, no cell is a
    number, and some row within the next three has a different dtype profile.
    Merged multi-row headers extend the depth while the next row is header-like
    and the row above it has repeated or missing names. Falls back to row 1.
    """
    limit = min(len(rows), HEADER_SCAN_ROWS)
    profiles = [_profile(r) for r in rows[:limit + HEADER_MAX_DEPTH + 3]]
    for i in range(limit):
        p = profiles[i]
        if not _headerish(p) or _is_title(p):
            continue
        following = profiles[i + 1 : i + 1 + HEADER_MAX_DEPTH + 3]
        if not any(q["filled"] > 0 and (q["number"] > 0 or q["date"] > 0 or q["filled"] != p["filled"] or not _headerish(q)) for q in following):
            continue
        depth = 1
        while depth < HEADER_MAX_DEPTH and i + depth < len(rows):
            above, below = rows[i + depth - 1], rows[i + depth]
            q = profiles[i + depth] if i + depth < len(profiles) else _profile(below)
            if not _headerish(q):
                break
            names_above = [cell_text(v) for v in above]
            filled_above = [n for n in names_above if n]
            widened = len(set(filled_above)) < len(filled_above) or len(filled_above) < q["filled"]
            if not widened:
                break
            # The row below must itself be followed by data, else it is the data.
            after = profiles[i + depth + 1] if i + depth + 1 < len(profiles) else None
            if after is None or after["filled"] == 0 or _headerish(after) and after["filled"] == q["filled"]:
                if after is not None and _headerish(after) and depth + 1 < HEADER_MAX_DEPTH:
                    depth += 1
                    continue
                break
            depth += 1
        return i + 1, depth
    return 1, 1


def _header_names(rows: list[list[Any]], header_row: int, depth: int, first_col: int) -> list[tuple[str, str]]:
    """``(name, letter)`` per column; multi-row headers joined with " / ",
    blank headers named after their column letter, duplicates numbered."""
    width = max((len(r) for r in rows[header_row - 1 : header_row - 1 + depth]), default=0)
    names: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for c in range(width):
        parts: list[str] = []
        for r in range(header_row - 1, header_row - 1 + depth):
            row = rows[r]
            text = cell_text(row[c]) if c < len(row) else ""
            if text and (not parts or parts[-1] != text):
                parts.append(text[:HEADER_MAX_CHARS])
        letter = get_column_letter(first_col + c)
        name = " / ".join(parts) or f"Column {letter}"
        count = seen.get(name, 0)
        seen[name] = count + 1
        if count:
            name = f"{name} ({count + 1})"
        names.append((name, letter))
    return names


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


def _describe_sheet(grid: _Grid) -> Sheet:
    rows = grid.rows
    base = dict(name=grid.name, index=grid.index, hidden=grid.hidden)
    if grid.chart or grid.hidden:
        reason = "chart sheet" if grid.chart else "hidden"
        return Sheet(**base, header_row=1, header_depth=1, columns=[], rows=[], dropped_rows=[], layout="rows", fingerprint=fingerprint_of([], 1), data_rows=0, skipped_reason=reason)
    # Trim trailing all-empty rows.
    while rows and all(cell_kind(v) == "empty" for v in rows[-1]):
        rows = rows[:-1]
    if not rows:
        return Sheet(**base, header_row=1, header_depth=1, columns=[], rows=[], dropped_rows=[], layout="rows", fingerprint=fingerprint_of([], 1), data_rows=0, skipped_reason="empty")

    header_row, depth = detect_header(rows)
    names = _header_names(rows, header_row, depth, grid.first_col)
    kept: list[dict] = []
    dropped: list[DroppedRow] = []
    for r in range(header_row - 1 + depth, len(rows)):
        row = rows[r]
        if all(cell_kind(v) == "empty" for v in row):
            dropped.append(DroppedRow(r + 1, "blank"))
            continue
        if _is_legend(row):
            dropped.append(DroppedRow(r + 1, "legend"))
            continue
        if _is_total(row):
            dropped.append(DroppedRow(r + 1, "total"))
            continue
        cells = {name: (cell_text(row[c]) if c < len(row) else "") for c, (name, _letter) in enumerate(names)}
        kept.append({"row": r + 1, "row_ref": f"{grid.name}!{get_column_letter(grid.first_col)}{r + 1}", "cells": cells})

    columns = [_describe_column(name, letter, c, kept, rows, header_row, depth) for c, (name, letter) in enumerate(names)]
    month_columns = [col for col in columns if col.month is not None]
    layout = "months_as_columns" if len(month_columns) >= MONTH_COLUMNS_MIN else "rows"
    fp = fingerprint_of([n for n, _ in names], depth)
    skipped = None
    if len(kept) < MIN_DATA_ROWS:
        skipped = "fewer than 2 data rows"
    return Sheet(**base, header_row=header_row, header_depth=depth, columns=columns, rows=kept, dropped_rows=dropped, layout=layout, fingerprint=fp, data_rows=len(kept), skipped_reason=skipped)


def _describe_column(name: str, letter: str, c: int, kept: list[dict], rows: list[list[Any]], header_row: int, depth: int) -> Column:
    raw = [rows[k["row"] - 1][c] if c < len(rows[k["row"] - 1]) else None for k in kept]
    kinds = [cell_kind(v) for v in raw]
    filled = [k for k in kinds if k != "empty"]
    n = len(kept)
    texts: list[str] = []
    seen: set[str] = set()
    units: list[str] = unit_tokens_in_header(name)
    for v, k in zip(raw, kinds):
        if k == "empty":
            continue
        t = cell_text(v)
        if t not in seen:
            seen.add(t)
            texts.append(t)
        if k == "text":
            u = unit_token_in_cell(t)
            if u and u not in units:
                units.append(u)
    if not filled:
        dtype = "empty"
    else:
        share = {k: sum(x == k for x in filled) / len(filled) for k in ("text", "number", "date")}
        best = max(share, key=share.get)
        dtype = best if share[best] >= 0.8 else "mixed"
    return Column(
        name=name,
        letter=letter,
        dtype=dtype,
        distinct=len(seen),
        samples=texts[:SAMPLE_VALUES],
        fill_ratio=round(len(filled) / n, 3) if n else 0.0,
        numeric_ratio=round(sum(k == "number" for k in filled) / len(filled), 3) if filled else 0.0,
        unit_tokens=units,
        month=month_of_header(name.split(" / ")[-1]) if " / " in name else month_of_header(name),
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def read_workbook_bytes(content: bytes, ext: str, name: str = "workbook") -> Workbook:
    """The description of a workbook held in memory. ``ext`` is xlsx, xls or csv."""
    ext = (ext or "").lower().lstrip(".")
    if ext == "xlsx" or ext == "xlsm":
        grids = _xlsx_grids(content)
    elif ext == "csv":
        grids = _csv_grids(content, Path(name).stem or "Sheet1")
    elif ext == "xls":
        grids = _xls_grids(content)
    else:
        raise ValueError("Only .xlsx, .xls and .csv files can be read.")
    wb = Workbook(name=name, ext=ext)
    for grid in grids[:MAPPING_SHEETS_PER_WORKBOOK]:
        wb.sheets.append(_describe_sheet(grid))
    for grid in grids[MAPPING_SHEETS_PER_WORKBOOK:]:
        wb.sheets.append(Sheet(name=grid.name, index=grid.index, hidden=grid.hidden, header_row=1, header_depth=1, columns=[], rows=[], dropped_rows=[], layout="rows", fingerprint=fingerprint_of([], 1), data_rows=0, skipped_reason=f"more than {MAPPING_SHEETS_PER_WORKBOOK} sheets"))
    return wb


def read_workbook(path: Union[str, Path]) -> Workbook:
    """The description of the workbook at ``path``."""
    p = Path(path)
    return read_workbook_bytes(p.read_bytes(), p.suffix.lstrip("."), p.name)
