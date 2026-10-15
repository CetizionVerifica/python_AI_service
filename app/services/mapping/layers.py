"""The layered mapper (XI2): a saved profile first, deterministic rules
second, the model last (``llm.py``, XI2 2/2). Each layer fills the parts of a
``SheetProposal`` that are still unresolved or below the confirmation
threshold, with a confidence and a source per part.

Everything a layer may name comes from the ``ContextView`` (the caller's
scoped import context) and the ``ActivityLookup``; anything else is dropped
and listed under ``not_in_context``. Sheet text is matched, never executed,
and never logged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.schemas.mapping import MAPPING_CONFIRM_BELOW, MAPPING_DISTINCT_VALUES_PER_COLUMN, MAPPING_TEXT_MAX_CHARS
from app.services.mapping.context import ACTIVITY_KEY, UNIT_KEY, VALUE_KEY, ActivityLookup, CategoryView, ContextView, FieldSpec
from app.services.mapping.match import best_match, contains_name, names_site, normalise, overlap
from app.services.mapping.synonyms import FIELD_LABEL_SYNONYMS, part_of_header
from app.services.workbook.reader import Column, Sheet, month_of_header, unit_tokens_in_header

NEEDS = MAPPING_CONFIRM_BELOW
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_FY_RE = re.compile(r"\bfy\s*'?(\d{4}|\d{2})(?:\s*[-/–]\s*(\d{2,4}))?", re.IGNORECASE)
_CURRENCY = {"inr", "usd", "eur", "gbp", "aed", "rs", "₹", "$", "€", "£"}
_CY_RE = re.compile(r"\b(cy|calendar\s+year|annual|yearly|year)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# A proposal under construction
# ---------------------------------------------------------------------------


@dataclass
class Part:
    """One resolved part: ``value`` is whatever the contract wants there."""

    value: object
    confidence: float
    source: str  # profile | rules | llm

    @property
    def resolved(self) -> bool:
        return self.value is not None and self.confidence >= NEEDS


@dataclass
class Draft:
    sheet: Sheet
    ctx: ContextView
    site_column: Optional[dict] = None
    category: Optional[Part] = None  # value: ResolvedCategory dict
    period: Optional[Part] = None  # value: SheetPeriod dict
    fields: dict[str, Part] = field(default_factory=dict)  # key → Part(value=column name or None)
    field_specs: dict[str, FieldSpec] = field(default_factory=dict)
    activities: dict[str, Part] = field(default_factory=dict)  # sheet text → Part(value=factor name or None)
    activity_categories: dict[str, int] = field(default_factory=dict)
    not_in_context: list[str] = field(default_factory=list)
    sources: set = field(default_factory=set)
    # The unit when no column holds it but the quantity's header or the sheet
    # title names it ("Charged (kg)", "... (kWh) ..."): the unit field then
    # needs no column (sent as skipped; rows take the header unit).
    header_unit: Optional[str] = None
    # Form fields that are the quantity under the form's own name on a
    # months-as-columns sheet: the month cells are the quantity, so the field
    # needs no column (sent as skipped, never the total column).
    cell_quantity: set = field(default_factory=set)

    def note_source(self, source: str) -> None:
        self.sources.add(source)

    def unresolved_fields(self) -> list[str]:
        return [k for k, p in self.fields.items() if not p.resolved]

    def to_proposal(self) -> dict:
        """The ``SheetProposal`` dict (contract §2) for this draft."""
        fields = []
        for key, spec in self.field_specs.items():
            part = self.fields.get(key)
            column = part.value if part else None
            fields.append(
                {
                    "key": key,
                    "label": spec.label,
                    "required": spec.required,
                    "column": column,
                    "confidence": round(part.confidence if part else 0.0, 3),
                    "source": part.source if part else "rules",
                    "skipped": column is None and (not spec.required or (key == UNIT_KEY and self.header_unit is not None) or key in self.cell_quantity),
                }
            )
        activities = []
        for text, part in self.activities.items():
            if not _fits(text):
                # Too long for the contract: left out; its rows reach the plan as TEXT_TOO_LONG.
                continue
            activities.append(
                {
                    "sheet_text": text,
                    "category_id": self.activity_categories.get(text) or _first_category_id(self),
                    "factor_name": part.value,
                    "confidence": round(part.confidence, 3),
                    "source": part.source,
                    "save_mapping": False,
                }
            )
        category = self.category.value if self.category else None
        if isinstance(category, dict) and category.get("kind") == "column":
            category = {**category, "values": [v for v in category["values"] if _fits(v["text"])]}
        site_column = self.site_column
        if site_column:
            site_column = {**site_column, "values": [v for v in site_column["values"] if _fits(v["text"])]}
        period = self.period.value if self.period else None
        required = [self.category.confidence if self.category else 0.0, self.period.confidence if self.period else 0.0]
        for key, spec in self.field_specs.items():
            if spec.required:
                required.append(self.fields[key].confidence if key in self.fields else 0.0)
        confidence = min(required) if required else 0.0
        if len(self.sources) == 0:
            source = "rules"
        elif len(self.sources) == 1:
            source = next(iter(self.sources))
        else:
            source = "mixed"
        return {
            "sheet_name": self.sheet.name,
            "fingerprint": self.sheet.fingerprint,
            "header_row": self.sheet.header_row,
            "data_rows": self.sheet.data_rows,
            "layout": self.sheet.layout,
            "site_id": self.ctx.site_id,
            "site_column": site_column,
            "category": category,
            "period": period,
            "fields": fields,
            "activities": activities,
            "not_in_context": [_clip(t) for t in dict.fromkeys(self.not_in_context)][:MAPPING_DISTINCT_VALUES_PER_COLUMN],
            "confidence": round(confidence, 3),
            "source": source,
            "excluded": category is None or period is None,
        }


def _fits(text: str) -> bool:
    return len(text or "") <= MAPPING_TEXT_MAX_CHARS


def _clip(text: str) -> str:
    """For display-only lists (not_in_context): cut to the contract's limit."""
    text = str(text or "")
    return text if _fits(text) else text[: MAPPING_TEXT_MAX_CHARS - 1] + "…"


def _first_category_id(draft: Draft) -> int:
    cat = draft.category.value if draft.category else None
    if isinstance(cat, dict) and cat.get("kind") == "fixed":
        return int(cat["id"])
    if isinstance(cat, dict) and cat.get("kind") == "column":
        ids = [v["id"] for v in cat.get("values") or [] if v.get("id")]
        if ids:
            return int(ids[0])
    return draft.ctx.categories[0].category_id if draft.ctx.categories else 0


def column_names(sheet: Sheet) -> list[str]:
    return [c.name for c in sheet.columns]


def column(sheet: Sheet, name: Optional[str]) -> Optional[Column]:
    return next((c for c in sheet.columns if c.name == name), None)


def distinct_values(sheet: Sheet, column_name: str, cap: int = MAPPING_DISTINCT_VALUES_PER_COLUMN) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in sheet.rows:
        t = r["cells"].get(column_name, "")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= cap:
                break
    return out


# ---------------------------------------------------------------------------
# Layer 1: a saved profile
# ---------------------------------------------------------------------------


def profile_layer(draft: Draft, profiles: list[dict], activities: ActivityLookup) -> bool:
    """Apply the saved ``SheetProposal`` of a profile with this sheet's
    fingerprint and site. Every part is re-checked against the current
    context and the sheet's columns; a part that no longer fits is left for
    the next layer. Returns whether a profile was used at all."""
    sheet, ctx = draft.sheet, draft.ctx
    hit = next(
        (p for p in profiles if str(p.get("fingerprint")) == sheet.fingerprint and int(p.get("site_id") or 0) == ctx.site_id),
        None,
    )
    if hit is None:
        return False
    saved = hit.get("sheet_proposal") or {}
    names = set(column_names(sheet))
    used = False

    cat = saved.get("category") or {}
    if cat.get("kind") == "fixed" and ctx.category(cat.get("id")):
        c = ctx.category(cat["id"])
        draft.category = Part({"kind": "fixed", "id": c.category_id, "label": c.name, "confidence": 1.0, "source": "profile"}, 1.0, "profile")
        used = True
    elif cat.get("kind") == "column" and cat.get("column") in names:
        values = []
        for text in distinct_values(sheet, cat["column"]):
            prev = next((v for v in cat.get("values") or [] if v.get("text") == text), None)
            c = ctx.category(prev.get("id")) if prev else None
            values.append({"text": text, "id": c.category_id if c else None, "confidence": 1.0 if c else 0.0, "source": "profile" if c else "rules"})
        if any(v["id"] for v in values):
            draft.category = Part({"kind": "column", "column": cat["column"], "values": values}, 1.0 if all(v["id"] for v in values) else 0.5, "profile")
            used = True

    period = saved.get("period") or {}
    kind = period.get("kind")
    if kind == "column" and period.get("column") in names:
        draft.period = Part(period, 1.0, "profile")
        used = True
    elif kind == "months_as_columns" and all(mc.get("column") in names for mc in period.get("columns") or []) and period.get("columns"):
        draft.period = Part(period, 1.0, "profile")
        used = True
    elif kind in ("monthly_fixed", "yearly"):
        # A fixed period belongs to the earlier file; the new file needs its own, so the rules decide.
        pass

    draft.field_specs = _field_specs(draft)
    for fm in saved.get("fields") or []:
        key = fm.get("key")
        if key in draft.field_specs and (fm.get("column") in names or fm.get("column") is None and fm.get("skipped")):
            draft.fields[key] = Part(fm.get("column"), 1.0 if fm.get("column") else 0.0, "profile")
            used = True

    cat_ids = {c.category_id for c in ctx.categories}
    wordings = _wordings(draft)
    for a in saved.get("activities") or []:
        text, factor = a.get("sheet_text"), a.get("factor_name")
        cid = a.get("category_id")
        if text in wordings and factor and cid in cat_ids and factor in activities.names(cid, factor):
            draft.activities[text] = Part(factor, 1.0, "profile")
            draft.activity_categories[text] = int(cid)
            used = True
    if saved.get("site_column") and (saved["site_column"].get("column") in names):
        draft.site_column = _site_column(sheet, saved["site_column"]["column"], ctx)
    if used:
        draft.note_source("profile")
    return used


# ---------------------------------------------------------------------------
# Layer 2: rules
# ---------------------------------------------------------------------------


def rules_layer(draft: Draft, activities: ActivityLookup) -> None:
    """Fill every part the profile left unresolved from header synonyms, the
    context's names, P23 mappings and factor names. Deterministic."""
    sheet, ctx = draft.sheet, draft.ctx
    taken: set[str] = set()  # columns already claimed by a part
    parts_by_column = {c.name: part_of_header(c.name) for c in sheet.columns}

    # Site column (informational): a header meaning "site" whose values name sites.
    if draft.site_column is None:
        for c in sheet.columns:
            if "site" in parts_by_column[c.name] and c.dtype == "text":
                draft.site_column = _site_column(sheet, c.name, ctx)
                break
    if draft.site_column:
        taken.add(draft.site_column["column"])
        for v in draft.site_column["values"]:
            if not v["matches_site"]:
                draft.not_in_context.append(v["text"])

    # Category: a category column, else the sheet name, else the only category.
    if draft.category is None or not draft.category.resolved:
        part = _category_rules(draft, parts_by_column, taken)
        if part is not None and (draft.category is None or part.confidence > draft.category.confidence):
            draft.category = part
            draft.note_source("rules")
    if draft.category and draft.category.value.get("kind") == "column":
        taken.add(draft.category.value["column"])

    # The fields the resolved category (or categories) want.
    draft.field_specs = _field_specs(draft)

    # Period.
    if draft.period is None or not draft.period.resolved:
        part = _period_rules(draft, parts_by_column, taken)
        if part is not None and (draft.period is None or part.confidence > draft.period.confidence):
            draft.period = part
            draft.note_source("rules")
    if draft.period:
        p = draft.period.value
        if p.get("kind") == "column":
            taken.add(p["column"])
        elif p.get("kind") == "months_as_columns":
            taken.update(mc["column"] for mc in p["columns"])

    # Activity column, then value and unit, then the form's fields.
    _field_rules(draft, parts_by_column, taken)

    # Activities: the activity column's distinct values against the context's names.
    _activity_rules(draft, activities)
    # An unsure category whose activity wordings all belong to one category is that category.
    if draft.category is not None and not draft.category.resolved and _category_from_activities(draft, activities):
        draft.field_specs = _field_specs(draft)
        _field_rules(draft, parts_by_column, taken)
        # The category changed: match the wordings again, keeping what a saved profile confirmed.
        draft.activities = {t: p for t, p in draft.activities.items() if p.source == "profile"}
        draft.activity_categories = {t: c for t, c in draft.activity_categories.items() if t in draft.activities}
        _activity_rules(draft, activities)


def _category_from_activities(draft: Draft, activities: ActivityLookup) -> bool:
    """When every activity wording matches names of exactly one category, fix that category (0.85)."""
    act_part = draft.fields.get(ACTIVITY_KEY)
    if not act_part or not act_part.value:
        return False
    wordings = distinct_values(draft.sheet, act_part.value)
    if not wordings:
        return False
    hits: dict[int, int] = {}
    for cat in draft.ctx.categories:
        names = activities.names(cat.category_id)
        n = sum(1 for w in wordings if best_match(w, names, 0.9))
        if n:
            hits[cat.category_id] = n
    full = [cid for cid, n in hits.items() if n == len(wordings)]
    if len(full) != 1:
        return False
    cat = draft.ctx.category(full[0])
    draft.category = Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": 0.85, "source": "rules"}, 0.85, "rules")
    draft.note_source("rules")
    return True


def _site_column(sheet: Sheet, column_name: str, ctx: ContextView) -> dict:
    values = []
    for text in distinct_values(sheet, column_name):
        values.append({"text": text, "matches_site": names_site(text, ctx.site_name, ctx.other_site_names)})
    return {"column": column_name, "values": values}


def _category_rules(draft: Draft, parts_by_column: dict[str, list[str]], taken: set[str]) -> Optional[Part]:
    sheet, ctx = draft.sheet, draft.ctx
    names = ctx.category_names
    # 1. A column whose header means "category" or whose values are category names.
    for c in sheet.columns:
        if c.name in taken or c.dtype != "text":
            continue
        values = distinct_values(sheet, c.name)
        if not values:
            continue
        matches = [(v, best_match(v, names, 0.6)) for v in values]
        hit_share = sum(1 for _, m in matches if m) / len(values)
        if hit_share >= 0.6 or ("category" in parts_by_column[c.name] and hit_share > 0):
            resolved = []
            for v, m in matches:
                cat = ctx.category_named(m[0]) if m else None
                resolved.append({"text": v, "id": cat.category_id if cat else None, "confidence": round(m[1] if m else 0.0, 3), "source": "rules"})
                if cat is None:
                    draft.not_in_context.append(v)
            conf = min((r["confidence"] for r in resolved), default=0.0)
            return Part({"kind": "column", "column": c.name, "values": resolved}, conf, "rules")
    # 2. The sheet name names a category.
    m = best_match(sheet.name, names, 0.5)
    if m:
        cat = ctx.category_named(m[0])
        return Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": round(m[1], 3), "source": "rules"}, m[1], "rules")
    for cat in ctx.categories:
        if contains_name(sheet.name, cat.name):
            return Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": 0.9, "source": "rules"}, 0.9, "rules")
    # 3. Header wordings that name a category ("kWh" → Purchased electricity is the model's job; only names count here).
    for c in sheet.columns:
        m = best_match(c.name, names, 0.8)
        if m and c.name not in taken:
            cat = ctx.category_named(m[0])
            return Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": 0.85, "source": "rules"}, 0.85, "rules")
    # 4. One category in scope: the only choice, but say so with a low confidence.
    if len(ctx.categories) == 1:
        cat = ctx.categories[0]
        return Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": 0.7, "source": "rules"}, 0.7, "rules")
    # 5. Best guess for the user to confirm: the first category, confidence 0.
    if ctx.categories:
        cat = ctx.categories[0]
        return Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": 0.0, "source": "rules"}, 0.0, "rules")
    return None


def _categories_in_play(draft: Draft) -> list[CategoryView]:
    cat = draft.category.value if draft.category else None
    if not isinstance(cat, dict):
        return []
    if cat.get("kind") == "fixed":
        c = draft.ctx.category(cat.get("id"))
        return [c] if c else []
    out = []
    for v in cat.get("values") or []:
        c = draft.ctx.category(v.get("id"))
        if c and c not in out:
            out.append(c)
    return out


def _field_specs(draft: Draft) -> dict[str, FieldSpec]:
    specs: dict[str, FieldSpec] = {ACTIVITY_KEY: FieldSpec(ACTIVITY_KEY, "Activity", required=False)}
    cats = _categories_in_play(draft)
    if not cats:
        specs[VALUE_KEY] = FieldSpec(VALUE_KEY, "Quantity", required=True, type="number")
        specs[UNIT_KEY] = FieldSpec(UNIT_KEY, "Unit", required=True)
        return specs
    for c in cats:
        for f in c.field_specs():
            if f.key not in specs:
                specs[f.key] = f
            elif f.required and not specs[f.key].required:
                specs[f.key] = f
    # A months-as-columns sheet carries its values in the month columns.
    if draft.sheet.layout == "months_as_columns" and VALUE_KEY in specs:
        specs[VALUE_KEY] = FieldSpec(VALUE_KEY, "Quantity", required=False, type="number")
    return specs


def _year_in(text: str) -> Optional[int]:
    m = _YEAR_RE.search(text or "")
    return int(m.group(1)) if m else None


def _period_rules(draft: Draft, parts_by_column: dict[str, list[str]], taken: set[str]) -> Optional[Part]:
    sheet = draft.sheet
    # Months as columns.
    if sheet.layout == "months_as_columns":
        cols = [c for c in sheet.columns if c.month]
        year_hint = _year_in(sheet.name) or next((_year_in(c.name) for c in sheet.columns if _year_in(c.name)), None)
        out, missing = [], False
        for c in cols:
            d = c.month.get("date")
            if d is None:
                y = c.month.get("year") or year_hint
                if y is None:
                    missing = True
                    y = date.today().year
                d = _month_end(y, c.month["month"])
            out.append({"column": c.name, "date": d})
        return Part({"kind": "months_as_columns", "columns": out}, 0.5 if missing else 0.95, "rules")
    # A date or month column.
    best: Optional[tuple[Column, float]] = None
    for c in sheet.columns:
        if c.name in taken or c.dtype == "empty":
            continue
        score = 0.0
        if "period" in parts_by_column[c.name]:
            score = 0.95 if c.dtype in ("date", "text") else 0.6
        elif c.dtype == "date":
            score = 0.85
        elif c.dtype == "text" and c.samples and all(month_of_header(s) for s in c.samples):
            score = 0.8
        if score and (best is None or score > best[1]):
            best = (c, score)
    if best:
        return Part({"kind": "column", "column": best[0].name}, best[1], "rules")
    # A yearly sheet: "FY 2024-25", "CY2024", "annual … 2024" in the sheet name or a header.
    texts = [sheet.name] + [c.name for c in sheet.columns]
    for t in texts:
        fy = _FY_RE.search(t)
        if fy:
            return Part({"kind": "yearly", "year_type": "FY", "year": _fy_end_year(fy)}, 0.9, "rules")
    for t in texts:
        y = _year_in(t)
        if y and _CY_RE.search(t):
            return Part({"kind": "yearly", "year_type": "CY", "year": y}, 0.85, "rules")
    for t in texts:
        y = _year_in(t)
        if y:
            return Part({"kind": "yearly", "year_type": "CY", "year": y}, 0.5, "rules")
    # Nothing says when: this month, for the user to change.
    today = date.today()
    return Part({"kind": "monthly_fixed", "date": _month_end(today.year, today.month)}, 0.0, "rules")


def _fy_end_year(m: re.Match) -> int:
    """ESG-lite files an FY year on Mar 31 of ``year``: the year the FY ends.
    "FY 2024-25" and "FY 2024/2025" end in 2025; a lone "FY 2025" or "FY25" is
    read the Indian way, as the year it ends."""
    start = int(m.group(1))
    start = start + 2000 if start < 100 else start
    if not m.group(2):
        return start
    end = int(m.group(2))
    if end < 100:
        end += (start // 100) * 100
        if end <= start:
            end += 100
    return end if start < end <= start + 1 else start + 1


def _month_end(year: int, month: int) -> str:
    import calendar

    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _field_rules(draft: Draft, parts_by_column: dict[str, list[str]], taken: set[str]) -> None:
    sheet = draft.sheet
    free = [c for c in sheet.columns if c.name not in taken]

    def claim(key: str, col: Column, conf: float) -> None:
        draft.fields[key] = Part(col.name, conf, "rules")
        taken.add(col.name)
        draft.note_source("rules")

    # Activity column: header means activity, else the text column with the most distinct values.
    if ACTIVITY_KEY not in draft.fields or not draft.fields[ACTIVITY_KEY].resolved:
        hit = next((c for c in free if "activity_name" in parts_by_column[c.name] and c.dtype == "text"), None)
        if hit:
            claim(ACTIVITY_KEY, hit, 0.95)
    # Unit: header means unit, else a text column whose values are all unit tokens.
    if UNIT_KEY in draft.field_specs and (UNIT_KEY not in draft.fields or not draft.fields[UNIT_KEY].resolved):
        hit = next((c for c in free if c.name not in taken and "activity_unit" in parts_by_column[c.name] and c.dtype == "text" and not c.unit_tokens and _mostly_units(c)), None)
        if hit is None:
            hit = next((c for c in free if c.name not in taken and c.dtype == "text" and c.samples and all(normalise(s) in _unit_words() for s in c.samples)), None)
            conf = 0.85 if hit else 0.0
        else:
            conf = 0.95
        if hit:
            claim(UNIT_KEY, hit, conf)
    # The form's own fields first (a distance form's "Distance (km)" is its distance field, not a generic quantity).
    _form_field_rules(draft, free, taken, claim)
    # Value: header means quantity, else the numeric column (unit token in its header counts extra).
    if VALUE_KEY in draft.field_specs and (VALUE_KEY not in draft.fields or not draft.fields[VALUE_KEY].resolved):
        cands = [c for c in free if c.name not in taken and c.dtype in ("number", "mixed", "text") and c.numeric_ratio >= 0.5]
        named = [c for c in cands if "activity_value" in parts_by_column[c.name] and not {"site", "category", "period"} & set(parts_by_column[c.name])]
        physical = [c for c in named if not (c.unit_tokens and set(c.unit_tokens) <= _CURRENCY)]
        if physical:
            claim(VALUE_KEY, physical[0], 0.95 if len(physical) == 1 else 0.7)
        elif named:
            claim(VALUE_KEY, named[0], 0.95 if len(named) == 1 else 0.7)
        elif len(cands) == 1:
            claim(VALUE_KEY, cands[0], 0.85 if cands[0].unit_tokens else 0.75)
        elif cands:
            with_unit = [c for c in cands if c.unit_tokens]
            if len(with_unit) == 1:
                claim(VALUE_KEY, with_unit[0], 0.7)
            else:
                claim(VALUE_KEY, cands[0], 0.4)
        else:
            # A text column holding "1,200 litre" is a value with its unit inside (mixed units).
            textual = [c for c in free if c.name not in taken and c.dtype == "text" and c.unit_tokens and "activity_value" in parts_by_column[c.name]]
            if textual:
                claim(VALUE_KEY, textual[0], 0.6)
    for key in (ACTIVITY_KEY, VALUE_KEY, UNIT_KEY):
        if key in draft.field_specs and key not in draft.fields:
            draft.fields[key] = Part(None, 0.0, "rules")
    _unit_from_header(draft)
    quantity_fields(draft)


def _mostly_units(col: Column) -> bool:
    """A column called "Unit" or "UoM" is the unit column only when its values are units (not equipment names)."""
    samples = [s for s in (col.samples or []) if str(s).strip()]
    if not samples:
        return True
    words = _unit_words()
    hits = sum(1 for s in samples if normalise(s) in words or normalise(s).replace(" ", "") in words)
    return hits * 2 >= len(samples)


def _unit_from_header(draft: Draft) -> None:
    """No unit column, but the quantity's header or the sheet title names the unit: no column is needed."""
    if UNIT_KEY not in draft.field_specs:
        return
    current = draft.fields.get(UNIT_KEY)
    if current is not None and current.value is not None:
        return
    value = draft.fields.get(VALUE_KEY)
    col = column(draft.sheet, value.value) if value is not None and value.value else None
    titles = getattr(draft.sheet, "title_units", None) or []
    # Only what the header itself says (unit_tokens also holds units seen in cells), as rows._header_unit reads it.
    in_header = unit_tokens_in_header(col.name) if col is not None else []
    unit = (in_header[0] if in_header else None) or (titles[0] if len(titles) == 1 else None)
    if unit:
        draft.header_unit = unit
        draft.fields[UNIT_KEY] = Part(None, 1.0, "rules")


def quantity_fields(draft: Draft) -> None:
    """A form without a calculation spec marks its quantity column required
    (ESG-lite's one-value rule: "Fuel consumption", "Amount of gas"). That
    column is the quantity under the form's own name, so with no column of its
    own it takes the quantity's column, as the data-entry page stores both. On
    a months-as-columns sheet the month cells are the quantity: the field needs
    no column and never takes another (a "FY Total" column is not a month)."""
    value = draft.fields.get(VALUE_KEY)
    period = draft.period.value if draft.period is not None and isinstance(draft.period.value, dict) else {}
    pivot = draft.sheet.layout == "months_as_columns" or period.get("kind") == "months_as_columns"
    if not pivot and (value is None or value.value is None):
        return
    for c in _categories_in_play(draft):
        if c.calc_mode not in (None, "single"):
            continue
        for f in c.fields:
            if not f.required or f.type != "number" or f.key not in draft.field_specs:
                continue
            current = draft.fields.get(f.key)
            if pivot:
                draft.fields[f.key] = Part(None, 1.0, "rules")
                draft.cell_quantity.add(f.key)
            elif current is None or current.value is None:
                draft.fields[f.key] = Part(value.value, value.confidence, value.source)


def _form_field_rules(draft: Draft, free: list[Column], taken: set[str], claim) -> None:
    """The form's own fields: by label, key, then the shared label synonyms."""
    sheet = draft.sheet
    for key, spec in draft.field_specs.items():
        if key in (ACTIVITY_KEY, VALUE_KEY, UNIT_KEY) or (key in draft.fields and draft.fields[key].resolved):
            continue
        pool = [c for c in free if c.name not in taken]
        hit = None
        conf = 0.0
        m = best_match(spec.label, [c.name for c in pool], 0.9) or best_match(key, [c.name for c in pool], 0.9)
        if m:
            hit, conf = column(sheet, m[0]), m[1]
        else:
            words = FIELD_LABEL_SYNONYMS.get(normalise(key)) or FIELD_LABEL_SYNONYMS.get(normalise(spec.label)) or []
            for c in pool:
                h = normalise(c.name)
                if any(h == w or f" {w} " in f" {h} " for w in words) or overlap(c.name, spec.label) >= 0.5:
                    hit, conf = c, 0.8
                    break
        if hit:
            claim(key, hit, conf)
        elif key not in draft.fields:
            draft.fields[key] = Part(None, 0.0, "rules")


_UNIT_WORDS: Optional[set[str]] = None


def _unit_words() -> set[str]:
    global _UNIT_WORDS
    if _UNIT_WORDS is None:
        from app.services.workbook.reader import _UNIT_TOKENS

        _UNIT_WORDS = {normalise(t) for t in _UNIT_TOKENS} | {"litre", "litres", "liter", "liters", "kwh", "kg", "tonne", "tonnes", "scm", "km", "nos", "mt", "kl", "l", "mwh", "t"}
    return _UNIT_WORDS


def _wordings(draft: Draft) -> dict[str, Optional[int]]:
    """The sheet's distinct activity wordings with the category each belongs to
    (None when the category column gave that row no category)."""
    sheet = draft.sheet
    act_part = draft.fields.get(ACTIVITY_KEY)
    cat = draft.category.value if draft.category else None
    cat_column = cat.get("column") if isinstance(cat, dict) and cat.get("kind") == "column" else None
    cat_by_text = {v["text"]: v.get("id") for v in (cat.get("values") or [])} if cat_column else {}
    fixed_id = int(cat["id"]) if isinstance(cat, dict) and cat.get("kind") == "fixed" else None

    if act_part and act_part.value:
        wordings: dict[str, Optional[int]] = {}
        for r in sheet.rows:
            text = r["cells"].get(act_part.value, "")
            if not text or text in wordings:
                continue
            wordings[text] = cat_by_text.get(r["cells"].get(cat_column, "")) if cat_column else fixed_id
            if len(wordings) >= MAPPING_DISTINCT_VALUES_PER_COLUMN:
                break
        return wordings
    # No activity column: the sheet (or its category) is one activity, named by the sheet.
    return {sheet.name: fixed_id}


def _activity_rules(draft: Draft, activities: ActivityLookup) -> None:
    ctx = draft.ctx
    wordings = _wordings(draft)
    for text, cid in wordings.items():
        if text in draft.activities and draft.activities[text].resolved:
            continue
        if cid is None:
            draft.activities[text] = Part(None, 0.0, "rules")
            continue
        draft.activity_categories[text] = int(cid)
        names = activities.names(cid)
        m = best_match(text, names, 0.9)
        if m is None and not activities.is_complete(ctx.category(cid)) if ctx.category(cid) else False:
            m = best_match(text, activities.names(cid, text), 0.9)
        if m:
            draft.activities[text] = Part(m[0], m[1], "rules")
            draft.note_source("rules")
        else:
            draft.activities[text] = Part(None, 0.0, "rules")
