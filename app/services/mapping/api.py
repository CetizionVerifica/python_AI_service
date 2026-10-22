"""What ``app/api/excel.py`` calls for the smart import: propose a mapping for
a stored document, and turn a confirmed proposal into plan rows."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.mapping import MAPPING_PROPOSE_PER_WINDOW, MAPPING_PROPOSE_WINDOW_MINUTES, MappingProposal
from app.services.excel_plan_client import PlanClient, SheetRows
from app.services.mapping.context import AcceptedUnitsLookup, ActivityLookup, context_view
from app.services.mapping.engine import propose
from app.services.mapping.rows import rows_from_proposal
from app.services.workbook.reader import Workbook, read_workbook_bytes

logger = logging.getLogger(__name__)

# propose: 10 per user per 10 minutes (01-contracts §6), per process.
_rate_lock = threading.Lock()
_rate: dict[int, list[float]] = {}


def check_propose_rate(user_id: Optional[int]) -> None:
    """Every attempt counts, failed ones too: a failing call still reads the
    file and may reach the model. The window is per process (see CLAUDE.md)."""
    if not user_id:
        # Only signed-in users reach /propose; never share one bucket between callers without an id.
        raise HTTPException(status_code=403, detail={"code": "IMPORT_FORBIDDEN", "message": "Sign in to use the smart import."})
    now = time.monotonic()
    window = MAPPING_PROPOSE_WINDOW_MINUTES * 60
    with _rate_lock:
        stamps = [t for t in _rate.get(int(user_id), []) if now - t < window]
        if len(stamps) >= MAPPING_PROPOSE_PER_WINDOW:
            raise HTTPException(status_code=429, detail={"code": "RATE_LIMITED", "message": f"At most {MAPPING_PROPOSE_PER_WINDOW} mapping proposals per {MAPPING_PROPOSE_WINDOW_MINUTES} minutes. Try again in a little while."})
        stamps.append(now)
        _rate[int(user_id)] = stamps


def reset_propose_rate() -> None:
    with _rate_lock:
        for k in list(_rate.keys()):
            _rate.pop(k, None)


def load_workbook_for(document_id: int) -> tuple[Workbook, Optional[str]]:
    """The stored document as a workbook description, plus its file name."""
    from app.core.database import get_uploaded_document_by_id
    from app.services.excel_parser import download_document_bytes

    content, ext = download_document_bytes(document_id)
    doc = get_uploaded_document_by_id(document_id) or {}
    name = doc.get("document_name") or f"upload-{document_id}"
    return read_workbook_bytes(content, ext, str(name)), (doc.get("document_name") or None)


def model_for_propose():
    """The configured ``MappingModel`` (XI2 2/2), or None for rules only."""
    try:
        from app.services.mapping.model import configured_model
    except ImportError:
        return None
    return configured_model()


def propose_document(
    client: PlanClient,
    *,
    document_id: int,
    site_id: int,
    company_id: Optional[int],
    sheet_categories: Optional[dict[str, int]] = None,
    only_sheets: Optional[list[str]] = None,
) -> MappingProposal:
    workbook, _ = load_workbook_for(document_id)
    if not workbook.readable_sheets:
        raise ValueError("The file has no sheet with at least two data rows.")
    context = client.get_context(company_id)
    try:
        ctx = context_view(context, site_id)
    except ValueError:
        raise HTTPException(status_code=403, detail={"code": "IMPORT_FORBIDDEN", "message": "You cannot import into this site."})
    if not ctx.categories:
        raise ValueError("This site has no categories assigned to import into.")
    profiles: list[dict] = []
    switch_checked = True
    for sheet in workbook.readable_sheets:
        if only_sheets is not None and sheet.name not in only_sheets:
            continue
        found = client.get_mapping_profiles(sheet.fingerprint, site_id, company_id)
        if found is None:
            # No mapping-profiles route (XI3 not deployed), so ESG-lite has not checked the
            # client's smart-import switch: the sheet must not go to a third-party model.
            switch_checked = False
        else:
            profiles.extend(found)
    activities = ActivityLookup(client, site_id, company_id)
    model = model_for_propose() if switch_checked else None
    if not switch_checked:
        logger.warning(f"propose document={document_id}: mapping profiles route missing, rules only")
    return propose(
        workbook, document_id, ctx, profiles, activities, model=model,
        sheet_categories=sheet_categories, only_sheets=only_sheets,
    )


def sheet_rows_from_proposal(payload: dict, principal=None, client: Optional[PlanClient] = None, company_id: Optional[int] = None) -> tuple[SheetRows, dict]:
    """``(rows, ctx)`` for ``/preview`` and ``/import`` when the payload carries a confirmed ``proposal``.

    The proposal's ``site_id`` must be one of the caller's sites, checked here
    as the top-level ``site_id`` is by ``enforce_site_scope`` (ESG-lite checks
    it again), since a Superadmin's ``company_id`` is looked up from it."""
    try:
        proposal = MappingProposal.model_validate(payload.get("proposal"))
    except ValidationError as e:
        raise ValueError(f"proposal is not a valid mapping: {e.errors()[0].get('msg') if e.errors() else 'invalid'}")
    if principal is not None and not principal.can_access_site(proposal.site_id):
        raise HTTPException(status_code=403, detail={"code": "IMPORT_FORBIDDEN", "message": "You cannot import into this site."})
    document_id = int(payload["document_id"])
    if proposal.document_id != document_id:
        raise ValueError("The proposal is for another document.")
    workbook, document_name = load_workbook_for(document_id)
    # With the caller's client, a row whose unit readings disagree takes the one its activity accepts (#183).
    accepted = AcceptedUnitsLookup(ActivityLookup(client, proposal.site_id, company_id)) if client is not None else None
    sheet = rows_from_proposal(workbook, proposal, accepted)
    return sheet, {
        "site_id": proposal.site_id,
        "category_id": None,
        "source_name": document_name or f"upload-{document_id}",
        "mapping": proposal.model_dump(),
    }
