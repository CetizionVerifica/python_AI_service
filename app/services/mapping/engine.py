"""``propose``: a workbook description for one site becomes a
``MappingProposal`` through the layers (profile → rules → model)."""
from __future__ import annotations

import logging
from typing import Optional

from app.schemas.mapping import MappingProposal
from app.services.mapping.context import ActivityLookup, ContextView
from app.services.mapping.layers import Draft, profile_layer, rules_layer
from app.services.workbook.reader import Workbook

logger = logging.getLogger(__name__)


class ModelStats:
    name: str = "none"
    calls: int = 0


def propose(workbook: Workbook, document_id: int, ctx: ContextView, profiles: list[dict], activities: ActivityLookup, model=None) -> MappingProposal:
    """One ``SheetProposal`` per readable sheet. ``model`` is the XI2 2/2 layer
    (``MappingModel``); None means rules only."""
    sheets = []
    profile_hits = 0
    calls = 0
    for sheet in workbook.readable_sheets:
        draft = Draft(sheet=sheet, ctx=ctx)
        if profile_layer(draft, profiles, activities):
            profile_hits += 1
        rules_layer(draft, activities)
        if model is not None:
            from app.services.mapping.llm import llm_layer

            calls += llm_layer(draft, activities, model)
        sheets.append(draft.to_proposal())
    telemetry = {"name": getattr(model, "name", "none") if model is not None else "none", "calls": calls, "cached_profile_hits": profile_hits}
    proposal = MappingProposal.model_validate({"document_id": int(document_id), "site_id": ctx.site_id, "sheets": sheets, "model": telemetry})
    logger.info(f"propose document={document_id} site={ctx.site_id} sheets={len(sheets)} profile_hits={profile_hits} model_calls={calls}")
    return proposal
