"""Layer 3: the model, for the parts the profile and the rules left
unresolved. One call per sheet at most, none when nothing is needed. The
answer is validated against the sheet's columns and the caller's context:
every id, key, column or factor name the context does not hold is dropped
and listed under ``not_in_context``. The model never overrides a part the
earlier layers resolved."""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

from app.services.mapping.context import ACTIVITY_KEY, UNIT_KEY, ActivityLookup
from app.services.mapping.layers import Draft, Part, _field_specs, _wordings, column_names, distinct_values
from app.services.mapping.match import best_match
from app.services.mapping.model import MappingModel, ModelAnswer, ModelError
from app.services.mapping.prompt import build_prompt, needed_parts

logger = logging.getLogger(__name__)

# A model confidence never reaches a confirmed profile's 1.0.
LLM_MAX_CONFIDENCE = 0.95
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _conf(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(LLM_MAX_CONFIDENCE, v))


def llm_layer(draft: Draft, activities: ActivityLookup, model: MappingModel) -> tuple[int, Optional[str]]:
    """Ask the model for the sheet's unresolved parts. Returns the number of
    calls (0 or 1) and, when the call gave no usable answer, an error code."""
    if not needed_parts(draft):
        return 0, None
    prompt = build_prompt(draft, activities)
    if prompt is None:
        logger.warning(f"mapping model skipped for sheet {draft.sheet.index}: the prompt does not fit the budget")
        return 0, None
    system, user = prompt
    try:
        answer = model.propose(system, user)
    except ModelError as e:
        code = error_code(e)
        logger.warning(f"mapping model gave no answer for sheet {draft.sheet.index}: {e} ({code})")
        return 1, code
    except Exception as e:  # an adapter bug or an SDK error it did not map: the rules result stands
        logger.warning(f"mapping model failed for sheet {draft.sheet.index}: {type(e).__name__}")
        return 1, error_code(e)
    apply_answer(draft, answer, activities)
    return 1, None


def error_code(e: BaseException) -> str:
    """A short code for a failed model call: the HTTP status of the provider's
    answer when there is one (``http_402``), else the type of the first error
    under the ``ModelError`` wrapper. Never the error's message, which can
    echo the request."""
    cause = e
    while isinstance(cause, ModelError) and cause.__cause__ is not None:
        cause = cause.__cause__
    seen = cause
    while seen is not None:
        status = getattr(seen, "status_code", None)
        if isinstance(status, int):
            return f"http_{status}"
        seen = seen.__cause__
    return type(cause).__name__[:60]


def _taken_columns(draft: Draft) -> set[str]:
    """Columns a resolved part already uses: the model may not put a second part on them."""
    taken: set[str] = set()
    if draft.site_column:
        taken.add(draft.site_column["column"])
    cat = draft.category.value if draft.category else None
    if isinstance(cat, dict) and cat.get("kind") == "column":
        taken.add(cat["column"])
    per = draft.period.value if draft.period else None
    if isinstance(per, dict) and per.get("kind") == "column":
        taken.add(per["column"])
    if isinstance(per, dict) and per.get("kind") == "months_as_columns":
        taken.update(mc["column"] for mc in per["columns"])
    taken.update(p.value for p in draft.fields.values() if p.value)
    return taken


def _sheet_category_ids(draft: Draft) -> set[int]:
    """The categories this sheet's rows can be in: the fixed one, or the ids the category column's values carry."""
    cat = draft.category.value if draft.category else None
    if not isinstance(cat, dict):
        return set()
    if cat.get("kind") == "fixed" and cat.get("id") is not None:
        return {int(cat["id"])}
    if cat.get("kind") == "column":
        return {int(v["id"]) for v in cat.get("values") or [] if v.get("id") is not None}
    return set()


def _valid_date(text: Optional[str]) -> bool:
    if not text or not _ISO.match(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def apply_answer(draft: Draft, answer: ModelAnswer, activities: ActivityLookup) -> None:
    ctx, sheet = draft.ctx, draft.sheet
    names = set(column_names(sheet))
    cat_ids = {c.category_id for c in ctx.categories}
    used = False

    if answer.site_column and draft.site_column is None:
        if answer.site_column in names:
            from app.services.mapping.layers import _site_column

            draft.site_column = _site_column(sheet, answer.site_column, ctx)
            for v in draft.site_column["values"]:
                if not v["matches_site"]:
                    draft.not_in_context.append(v["text"])
            used = True
        else:
            draft.not_in_context.append(answer.site_column)

    if answer.category and (draft.category is None or not draft.category.resolved):
        a = answer.category
        if a.kind == "fixed":
            cat = ctx.category(a.category_id)
            if cat:
                draft.category = Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": _conf(a.confidence), "source": "llm"}, _conf(a.confidence), "llm")
                used = True
            elif a.category_id is not None:
                draft.not_in_context.append(f"category {a.category_id}")
        elif a.kind == "column" and a.column in names:
            # The column's own values decide the list: the model only says which category each one is.
            said = {v.text: v for v in a.values}
            values = []
            for text in distinct_values(sheet, a.column):
                v = said.get(text)
                cat = ctx.category(v.category_id) if v is not None else None
                if v is not None and cat is None and v.category_id is not None:
                    draft.not_in_context.append(f"category {v.category_id}")
                values.append({"text": text, "id": cat.category_id if cat else None, "confidence": _conf(v.confidence) if cat else 0.0, "source": "llm"})
            if values:
                conf = min(x["confidence"] for x in values)
                draft.category = Part({"kind": "column", "column": a.column, "values": values}, min(conf, _conf(a.confidence) or conf), "llm")
                used = True
        elif a.kind == "column" and a.column:
            draft.not_in_context.append(a.column)
    draft.field_specs = _field_specs(draft)

    if answer.period and answer.period.kind == "column" and answer.period.column and answer.period.column not in names:
        draft.not_in_context.append(answer.period.column)
    if answer.period and (draft.period is None or not draft.period.resolved):
        p = answer.period
        value = None
        if p.kind == "column" and p.column in names:
            value = {"kind": "column", "column": p.column}
        elif p.kind == "monthly_fixed" and _valid_date(p.date):
            value = {"kind": "monthly_fixed", "date": p.date}
        elif p.kind == "yearly" and p.year_type in ("CY", "FY") and p.year and 2000 <= p.year <= 2100:
            value = {"kind": "yearly", "year_type": p.year_type, "year": p.year}
        elif p.kind == "months_as_columns":
            cols = [{"column": mc.column, "date": mc.date} for mc in p.columns if mc.column in names and _valid_date(mc.date)]
            if cols:
                value = {"kind": "months_as_columns", "columns": cols}
        if value is not None:
            draft.period = Part(value, _conf(p.confidence), "llm")
            used = True

    taken = _taken_columns(draft)
    for f in answer.fields:
        if f.column is None:
            continue
        if f.column not in names:
            draft.not_in_context.append(f.column)
            continue
        if f.key not in draft.field_specs:
            draft.not_in_context.append(f"field {f.key}")
            continue
        current = draft.fields.get(f.key)
        if current is not None and current.resolved:
            continue
        if f.key == UNIT_KEY and draft.header_unit:
            continue  # the header or title already names the unit
        if f.key in draft.cell_quantity:
            continue  # the month cells are the quantity
        if f.column in taken and not (current is not None and current.value == f.column):
            continue
        if current is not None and current.value:
            taken.discard(current.value)
        draft.fields[f.key] = Part(f.column, _conf(f.confidence), "llm")
        taken.add(f.column)
        used = True

    wordings = _wordings(draft)
    for a in answer.activities:
        if a.sheet_text not in wordings:
            # Only the sheet's own wordings can be matched; an invented one would be learned by XI3.
            continue
        current = draft.activities.get(a.sheet_text)
        if current is not None and current.resolved:
            continue
        cid = a.category_id if a.category_id in cat_ids else draft.activity_categories.get(a.sheet_text)
        if a.category_id is not None and a.category_id not in cat_ids:
            draft.not_in_context.append(f"category {a.category_id}")
        allowed = _sheet_category_ids(draft)
        if wordings.get(a.sheet_text) is not None:
            allowed = {wordings[a.sheet_text]}
        if cid is None or (allowed and cid not in allowed):
            continue
        if a.factor_name is None:
            continue
        m = best_match(a.factor_name, activities.names(cid), 0.97) or best_match(a.factor_name, activities.names(cid, a.factor_name), 0.97)
        if m is None:
            draft.not_in_context.append(a.factor_name)
            continue
        draft.activities[a.sheet_text] = Part(m[0], _conf(a.confidence), "llm")
        draft.activity_categories[a.sheet_text] = int(cid)
        used = True

    # An activity column the model found: its wordings need matching too.
    act = draft.fields.get(ACTIVITY_KEY)
    if act and act.value and act.source == "llm":
        from app.services.mapping.layers import _activity_rules

        _activity_rules(draft, activities)
        for a in answer.activities:
            if a.sheet_text in draft.activities and not draft.activities[a.sheet_text].resolved and a.factor_name:
                cid = a.category_id if a.category_id in cat_ids else draft.activity_categories.get(a.sheet_text)
                allowed = _sheet_category_ids(draft)
                if cid is None or (allowed and cid not in allowed):
                    continue
                m = best_match(a.factor_name, activities.names(cid), 0.97)
                if m:
                    draft.activities[a.sheet_text] = Part(m[0], _conf(a.confidence), "llm")
    if used:
        draft.note_source("llm")
