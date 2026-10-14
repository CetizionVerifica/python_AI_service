"""``rows_from_proposal``: a confirmed ``MappingProposal`` plus the workbook
description become ``ImportRow``s for ESG-lite's plans API. Deterministic,
no model, no network. Nothing is computed here: values and units travel as
written and ESG-lite's engine does the rest.

Rows this service can tell are not importable are kept back as local
issues, exactly as the IM11 contributor path does for ``MISSING_*``:
``SITE_NOT_ALLOWED`` when the site column names another site,
``CATEGORY_NOT_ASSIGNED`` when the category column's text resolved to no
category, ``PIVOT_EMPTY_CELL`` (info) for a blank month cell. ESG-lite
re-checks every row against the caller's scope anyway.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

from app.schemas.mapping import CATEGORY_NOT_ASSIGNED, PIVOT_EMPTY_CELL, SITE_NOT_ALLOWED, MappingProposal, SheetProposal
from app.services.excel_plan_client import ROWS_PER_PLAN, LocalIssue, SheetRows, _number
from app.services.mapping.context import ACTIVITY_KEY, UNIT_KEY, VALUE_KEY
from app.services.workbook.reader import Sheet, Workbook, month_of_header

_ROW_NUM_RE = re.compile(r"!([A-Z]+)(\d+)$")
_VALUE_WITH_UNIT_RE = re.compile(r"^\s*([+-]?[\d,]*\.?\d+)\s*([A-Za-z³₹$€£]{1,8})?\s*$")


def _row_number(row_ref: str) -> int:
    m = _ROW_NUM_RE.search(row_ref)
    return int(m.group(2)) if m else 0


def _issue(row_ref: str, category_text: Optional[str], code: str, reason: str, cells: dict, severity: str = "error") -> LocalIssue:
    return LocalIssue(_row_number(row_ref), row_ref, category_text, code, reason, dict(cells), severity)


def _period_for(cell: str, proposal: SheetProposal) -> Optional[dict]:
    p = proposal.period
    if p.kind == "monthly_fixed":
        return {"kind": "monthly", "date": p.date}
    if p.kind == "yearly":
        return {"kind": "yearly", "year_type": p.year_type, "year": p.year}
    if p.kind == "column":
        text = (cell or "").strip()
        if not text:
            return None
        m = month_of_header(text)
        if m and m.get("date") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return {"kind": "monthly", "date": m["date"]}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return {"kind": "monthly", "date": text}
        try:
            ts = pd.to_datetime(text, dayfirst=True, errors="coerce")
        except Exception:
            ts = None
        if ts is None or pd.isna(ts):
            return None
        return {"kind": "monthly", "date": ts.strftime("%Y-%m-%d")}
    return None


def _split_value_unit(text: str) -> tuple[Optional[float], Optional[str]]:
    """"1,200 litre" → (1200.0, "litre"); "1200" → (1200.0, None); "abc" → (None, None)."""
    m = _VALUE_WITH_UNIT_RE.match(text or "")
    if not m:
        return None, None
    number = _number(m.group(1))
    unit = m.group(2)
    return number, (unit if unit else None)


def rows_from_sheet(sheet: Sheet, proposal: SheetProposal) -> SheetRows:
    """The ``ImportRow``s of one sheet under its confirmed proposal."""
    fields = {f.key: f for f in proposal.fields if f.column and not f.skipped}
    activity_col = fields[ACTIVITY_KEY].column if ACTIVITY_KEY in fields else None
    value_col = fields[VALUE_KEY].column if VALUE_KEY in fields else None
    unit_col = fields[UNIT_KEY].column if UNIT_KEY in fields else None
    extra_cols = {k: f.column for k, f in fields.items() if k not in (ACTIVITY_KEY, VALUE_KEY, UNIT_KEY)}
    matches = {a.sheet_text: a for a in proposal.activities}
    single_activity = proposal.activities[0].sheet_text if not activity_col and len(proposal.activities) == 1 else None
    cat = proposal.category
    cat_by_text = {v.text: v.id for v in cat.values} if cat.kind == "column" else {}
    site_col = proposal.site_column.column if proposal.site_column else None
    site_ok = {v.text: v.matches_site for v in proposal.site_column.values} if proposal.site_column else {}
    period_col = proposal.period.column if proposal.period.kind == "column" else None
    month_cols = [(mc.column, mc.date) for mc in proposal.period.columns] if proposal.period.kind == "months_as_columns" else []
    letters = {c.name: c.letter for c in sheet.columns}

    rows: list[dict] = []
    local: list[LocalIssue] = []
    for r in sheet.rows:
        cells, row_ref = r["cells"], r["row_ref"]
        activity_text = (cells.get(activity_col, "") if activity_col else single_activity) or ""
        category_text = cells.get(cat.column, "") if cat.kind == "column" else None

        if site_col:
            site_text = cells.get(site_col, "")
            # A wording the proposal never saw (beyond the distinct cap) is not this site either.
            ok = site_ok.get(site_text, False)
            if site_text and not ok:
                local.append(_issue(row_ref, activity_text or None, SITE_NOT_ALLOWED, "This row is for a site other than the one chosen for this upload.", cells))
                continue
        if cat.kind == "fixed":
            category_id: Optional[int] = cat.id
        else:
            category_id = cat_by_text.get(category_text or "")
        if not category_id:
            local.append(_issue(row_ref, activity_text or None, CATEGORY_NOT_ASSIGNED, "The row's category is not one of this site's categories.", cells))
            continue

        match = matches.get(activity_text)
        meta_source = match.source if match else (proposal.source if proposal.source != "mixed" else "rules")
        meta_conf = min(proposal.confidence, match.confidence) if match else proposal.confidence
        base: dict[str, Any] = {"site_id": proposal.site_id, "category_id": int(category_id), "activity_name": activity_text, "meta": {"source": meta_source, "confidence": round(meta_conf, 3)}}
        extra = {k: cells.get(c, "") for k, c in extra_cols.items() if cells.get(c, "")}
        unit = cells.get(unit_col, "").strip() if unit_col else ""

        if month_cols:
            for col, d in month_cols:
                text = cells.get(col, "")
                ref = f"{sheet.name}!{letters.get(col, 'A')}{r['row']}"
                if not text.strip():
                    local.append(_issue(ref, activity_text or None, PIVOT_EMPTY_CELL, "Blank month cell; no row made.", {**cells, "month": col}, "info"))
                    continue
                value, unit_in_cell = _split_value_unit(text)
                row = {**base, "row_ref": ref, "period": {"kind": "monthly", "date": d}, "activity_unit": unit or unit_in_cell or _header_unit(sheet, col) or ""}
                if value is not None:
                    row["activity_value"] = value
                else:
                    row["fields"] = {VALUE_KEY: text}
                if extra:
                    row["fields"] = {**row.get("fields", {}), **extra}
                _append(rows, local, row, activity_text, cells)
            continue

        period = _period_for(cells.get(period_col, "") if period_col else "", proposal)
        row = {**base, "row_ref": row_ref, "period": period}
        value_text = cells.get(value_col, "") if value_col else ""
        value, unit_in_cell = _split_value_unit(value_text) if value_text else (None, None)
        if value_col and value_text and value is None:
            local.append(_issue(row_ref, activity_text or None, "VALUE_NOT_NUMERIC", f"The activity value {value_text!r} is not a number.", cells))
            continue
        row["activity_unit"] = unit or unit_in_cell or (_header_unit(sheet, value_col) if value_col else None) or _fields_unit(sheet, extra_cols) or ""
        if value is not None:
            row["activity_value"] = value
        if extra:
            row["fields"] = extra
        _append(rows, local, row, activity_text, cells)

    if len(rows) > ROWS_PER_PLAN:
        raise ValueError(f"The sheet has {len(rows)} rows to import; one upload takes at most {ROWS_PER_PLAN}. Split the file.")
    return SheetRows(rows=rows, local_issues=local, total_rows=len(sheet.rows), not_selected=0)


def _header_unit(sheet: Sheet, column_name: Optional[str]) -> Optional[str]:
    """The unit written in a column's header ("Qty (L)", "kWh"), as the unit for its values."""
    col = next((c for c in sheet.columns if c.name == column_name), None)
    if col is None:
        return None
    header_only = [t for t in col.unit_tokens if t in (col.name.lower())]
    return header_only[0] if header_only else None


def _fields_unit(sheet: Sheet, extra_cols: dict[str, str]) -> Optional[str]:
    """With no value column, the unit in a mapped form field's header ("Distance (km)")."""
    for col in extra_cols.values():
        u = _header_unit(sheet, col)
        if u:
            return u
    return None


def _append(rows: list[dict], local: list[LocalIssue], row: dict, activity_text: str, cells: dict) -> None:
    """The row, or the local issue the plans API would refuse it with."""
    ref = row["row_ref"]
    if not activity_text:
        local.append(_issue(ref, None, "MISSING_CATEGORY", "No activity wording in this row.", cells))
        return
    if row.get("period") is None:
        local.append(_issue(ref, activity_text, "MISSING_PERIOD", "No date or month in this row.", cells))
        return
    if not row.get("activity_unit"):
        local.append(_issue(ref, activity_text, "MISSING_UNIT", "Missing activity unit.", cells))
        return
    if row.get("activity_value") is None and not row.get("fields"):
        local.append(_issue(ref, activity_text, "MISSING_VALUE", "No activity value or form fields in this row.", cells))
        return
    rows.append(row)


def rows_from_proposal(workbook: Workbook, proposal: MappingProposal) -> SheetRows:
    """Every sheet's rows under the confirmed proposal, excluded sheets left out."""
    by_name = {s.name: s for s in workbook.sheets}
    rows: list[dict] = []
    local: list[LocalIssue] = []
    total = 0
    for sp in proposal.sheets:
        if sp.excluded:
            continue
        sheet = by_name.get(sp.sheet_name)
        if sheet is None or sheet.skipped_reason:
            continue
        if sheet.fingerprint != sp.fingerprint:
            raise ValueError(f"The sheet {sp.sheet_name!r} no longer matches the confirmed mapping. Upload the file again.")
        part = rows_from_sheet(sheet, sp)
        rows.extend(part.rows)
        local.extend(part.local_issues)
        total += part.total_rows
    if len(rows) > ROWS_PER_PLAN:
        raise ValueError(f"The workbook has {len(rows)} rows to import; one upload takes at most {ROWS_PER_PLAN}. Split the file.")
    return SheetRows(rows=rows, local_issues=local, total_rows=total, not_selected=0)
