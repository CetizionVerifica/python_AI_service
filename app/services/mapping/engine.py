"""``propose``: a workbook description for one site becomes a
``MappingProposal`` through the layers (profile → rules → model)."""
from __future__ import annotations

import logging
from typing import Optional

from app.schemas.mapping import MappingProposal
from app.services.mapping.context import ActivityLookup, ContextView
from app.services.mapping.layers import Draft, Part, profile_layer, quantity_fields, rules_layer
from app.services.workbook.reader import Workbook

logger = logging.getLogger(__name__)


class ModelStats:
    name: str = "none"
    calls: int = 0


def propose(
    workbook: Workbook,
    document_id: int,
    ctx: ContextView,
    profiles: list[dict],
    activities: ActivityLookup,
    model=None,
    sheet_categories: Optional[dict[str, int]] = None,
    only_sheets: Optional[list[str]] = None,
) -> MappingProposal:
    """One ``SheetProposal`` per readable sheet. ``model`` is the XI2 2/2 layer
    (``MappingModel``); None means rules only.

    ``sheet_categories`` (sheet name -> category id) is the category the user
    chose on the confirm screen: it is taken as resolved (confidence 1), only
    saved profiles of that category may apply, and the rules and the model
    then fill that category's own fields and activities. ``only_sheets``
    limits the answer to the named sheets (the screen re-asks for one sheet
    after a category change). A chosen category outside the context is a
    ValueError."""
    sheets = []
    profile_hits = 0
    calls = 0
    chosen_by_sheet = sheet_categories or {}
    for sheet in workbook.readable_sheets:
        if only_sheets is not None and sheet.name not in only_sheets:
            continue
        draft = Draft(sheet=sheet, ctx=ctx)
        sheet_profiles = profiles
        chosen = chosen_by_sheet.get(sheet.name)
        if chosen is not None:
            draft.category = user_category(ctx, chosen)
            sheet_profiles = [p for p in profiles if _profile_category(p) in (None, chosen)]
        if profile_layer(draft, sheet_profiles, activities):
            profile_hits += 1
        rules_layer(draft, activities)
        if model is None:
            sheets.append(draft.to_proposal())
            continue
        from app.schemas.mapping import SheetProposal
        from app.services.mapping.llm import llm_layer

        rules_only = draft.to_proposal()
        try:
            calls += llm_layer(draft, activities, model)
            # The model may have found the quantity only now.
            quantity_fields(draft)
            sheet_proposal = draft.to_proposal()
            SheetProposal.model_validate(sheet_proposal)
        except Exception as e:  # a model answer the contract refuses: this sheet keeps the profile and rules result
            logger.warning(f"propose document={document_id} sheet={sheet.index}: model answer dropped ({type(e).__name__})")
            calls = max(calls, 1)
            sheet_proposal = rules_only
        sheets.append(sheet_proposal)
    if not sheets:
        raise ValueError("None of the named sheets can be read in this workbook.")
    telemetry = {"name": str(getattr(model, "name", "none"))[:100] if model is not None else "none", "calls": calls, "cached_profile_hits": profile_hits}
    proposal = MappingProposal.model_validate({"document_id": int(document_id), "site_id": ctx.site_id, "sheets": sheets, "model": telemetry})
    logger.info(f"propose document={document_id} site={ctx.site_id} sheets={len(sheets)} profile_hits={profile_hits} model_calls={calls}")
    return proposal


def user_category(ctx: ContextView, category_id: int) -> Part:
    """The category the user picked for a sheet, as a resolved part."""
    cat = ctx.category(int(category_id))
    if cat is None:
        raise ValueError(f"Category {category_id} is not one of this site's categories.")
    return Part({"kind": "fixed", "id": cat.category_id, "label": cat.name, "confidence": 1.0, "source": "rules"}, 1.0, "rules")


def _profile_category(profile: dict) -> Optional[int]:
    """The fixed category a saved profile maps its sheet to (None for a category column)."""
    cat = (profile.get("sheet_proposal") or {}).get("category") or {}
    if cat.get("kind") == "fixed" and cat.get("id") is not None:
        return int(cat["id"])
    return int(profile["category_id"]) if profile.get("category_id") else None
