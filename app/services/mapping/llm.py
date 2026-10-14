"""Layer 3: the model, for the parts the profile and the rules left
unresolved. One call per sheet at most, none when nothing is needed. The
answer is validated against the sheet's columns and the caller's context:
every id, key, column or factor name the context does not hold is dropped
and listed under ``not_in_context``. The model never overrides a part the
earlier layers resolved."""
from __future__ import annotations

import logging
import re

from app.services.mapping.context import ACTIVITY_KEY, ActivityLookup
from app.services.mapping.layers import Draft, Part, _field_specs, column_names
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


def llm_layer(draft: Draft, activities: ActivityLookup, model: MappingModel) -> int:
    """Ask the model for the sheet's unresolved parts. Returns the number of calls (0 or 1)."""
    if not needed_parts(draft):
        return 0
    system, user = build_prompt(draft, activities)
    try:
        answer = model.propose(system, user)
    except ModelError as e:
        logger.warning(f"mapping model gave no answer for sheet {draft.sheet.index}: {e}")
        return 1
    apply_answer(draft, answer, activities)
    return 1


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
            values = []
            for v in a.values:
                cat = ctx.category(v.category_id)
                if cat is None and v.category_id is not None:
                    draft.not_in_context.append(v.text)
                values.append({"text": v.text, "id": cat.category_id if cat else None, "confidence": _conf(v.confidence) if cat else 0.0, "source": "llm"})
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
        elif p.kind == "monthly_fixed" and p.date and _ISO.match(p.date):
            value = {"kind": "monthly_fixed", "date": p.date}
        elif p.kind == "yearly" and p.year_type in ("CY", "FY") and p.year and 2000 <= p.year <= 2100:
            value = {"kind": "yearly", "year_type": p.year_type, "year": p.year}
        elif p.kind == "months_as_columns":
            cols = [{"column": mc.column, "date": mc.date} for mc in p.columns if mc.column in names and _ISO.match(mc.date or "")]
            if cols:
                value = {"kind": "months_as_columns", "columns": cols}
        if value is not None:
            draft.period = Part(value, _conf(p.confidence), "llm")
            used = True

    for f in answer.fields:
        if f.column is None:
            continue
        if f.column not in names:
            draft.not_in_context.append(f.column)
            continue
        if f.key not in draft.field_specs:
            continue
        current = draft.fields.get(f.key)
        if current is not None and current.resolved:
            continue
        draft.fields[f.key] = Part(f.column, _conf(f.confidence), "llm")
        used = True

    for a in answer.activities:
        current = draft.activities.get(a.sheet_text)
        if current is not None and current.resolved:
            continue
        cid = a.category_id if a.category_id in cat_ids else draft.activity_categories.get(a.sheet_text)
        if cid is None:
            if a.category_id is not None:
                draft.not_in_context.append(f"category {a.category_id}")
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
                m = best_match(a.factor_name, activities.names(cid), 0.97) if cid else None
                if m:
                    draft.activities[a.sheet_text] = Part(m[0], _conf(a.confidence), "llm")
    if used:
        draft.note_source("llm")
