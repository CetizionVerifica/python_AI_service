"""PCF AI assist (E2). Nothing here saves to ESG-lite: every endpoint returns
suggestions with a confidence and a reason for a person to check first.

Callers: Managers and Superadmins, the roles ESG-lite lets use /pcf/*.
"""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.auth import Principal, SUPERADMIN, require_user
from app.core.config import settings
from app.schemas.pcf import BomResponse, FactorSheetResponse, MatchRequest, MatchResponse
from app.services import pcf_bom, pcf_match, pcf_sheet

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pcf", tags=["pcf"])

PCF_ROLES = {"Manager", SUPERADMIN}


def require_pcf_role(principal: Principal = Depends(require_user)) -> Principal:
    if principal.role not in PCF_ROLES:
        raise HTTPException(status_code=403, detail="Not allowed")
    return principal


def _llm(messages: list[dict]) -> str:
    from app.services.llm import _call_openrouter

    return _call_openrouter(messages)


async def _read_upload(file: UploadFile, allowed: tuple[str, ...]) -> tuple[bytes, str]:
    filename = file.filename or ""
    if not filename.lower().endswith(allowed):
        raise HTTPException(status_code=400, detail=f"Use a {' or '.join(allowed)} file.")
    limit = settings.MAX_FILE_MB * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail=f"The file is over {settings.MAX_FILE_MB} MB.")
    if not content:
        raise HTTPException(status_code=400, detail="The file is empty.")
    return content, filename


@router.post("/material-factors/parse-excel", response_model=FactorSheetResponse)
async def parse_material_factor_sheet(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
    mapping: Optional[str] = Form(None, description='JSON {"<field>": "<header>" | null} the person picked'),
    principal: Principal = Depends(require_pcf_role),
):
    """A material-factor spreadsheet in any layout → MaterialFactor rows for C04's import.

    Columns are matched by header name; the AI only places the name and value
    columns when headers alone can't. Send `mapping` to re-read the same file
    with the person's column choices, and `sheet_name` to read another sheet.
    """
    content, filename = await _read_upload(file, (".xlsx", ".csv"))
    override = None
    if mapping:
        try:
            override = json.loads(mapping)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="mapping must be JSON")
        if not isinstance(override, dict) or not all(v is None or isinstance(v, str) for v in override.values()):
            raise HTTPException(status_code=422, detail='mapping must look like {"name": "Material"}')
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: pcf_sheet.read_factor_sheet(content, filename, sheet_name, override, call_llm=_llm),
        )
    except pcf_sheet.SheetError as e:
        raise HTTPException(status_code=422, detail=str(e))
    logger.info(
        "PCF factor sheet read by user %s: %s rows, %s with problems, ai=%s",
        principal.user_id, result["total_rows"], result["rows_with_problems"], result["ai_used"],
    )
    return result


def _extract_text(content: bytes, filename: str) -> str:
    """Text of a PDF through the invoice OCR path (digital first, Tesseract after)."""
    import os
    import uuid
    from pathlib import Path

    from app.services import ocr

    path = Path(settings.TEMP_DIR) / f"{uuid.uuid4()}{os.path.splitext(filename)[1].lower()}"
    path.write_bytes(content)
    try:
        return ocr.extract_text(path)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _store_evidence(content: bytes, filename: str, content_type: Optional[str], study_id: Optional[int], user_id: Optional[int]) -> Optional[dict]:
    """Keep the raw BOM as evidence (Cloudinary when configured, else this instance's temp dir)."""
    import uuid
    from pathlib import Path

    from app.core import cloudinary_service
    from app.core.database import insert_pcf_evidence

    local_path = Path(settings.TEMP_DIR) / f"{uuid.uuid4()}-{Path(filename).name}"
    local_path.write_bytes(content)
    stored_url, public_id, where = str(local_path), None, "local"
    if cloudinary_service.is_configured():
        try:
            cloud = cloudinary_service.upload_file(local_path, folder="pcf-evidence")
            stored_url, public_id, where = cloud["secure_url"], cloud["public_id"], "cloud"
        except Exception:
            logger.warning("Cloudinary upload failed for %s; keeping the local copy", filename, exc_info=True)
    else:
        logger.warning("Cloudinary is not configured; PCF evidence %s stays on this instance only", filename)
    try:
        row = insert_pcf_evidence("bom", filename, stored_url, public_id, content_type, len(content), study_id, user_id)
    except Exception:
        logger.warning("Couldn't record PCF evidence for %s", filename, exc_info=True)
        if public_id:
            try:
                cloudinary_service.delete_file(public_id)
            except Exception:
                logger.warning("Orphaned Cloudinary asset %s", public_id, exc_info=True)
        return None
    if where == "cloud":
        try:
            local_path.unlink()
        except OSError:
            pass
    return {"id": row["id"], "document_name": filename, "url": stored_url, "stored": where}


@router.post("/parse-bom", response_model=BomResponse)
async def parse_bom(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
    mapping: Optional[str] = Form(None, description='JSON {"<field>": "<header>" | null} the person picked'),
    batch_units: float = Form(1.0, description="How many declared units the file's quantities cover"),
    mass_per_unit_kg: Optional[float] = Form(None, description="The product's mass per declared unit, to sanity-check A1"),
    study_id: Optional[int] = Form(None, description="PcfStudy the evidence belongs to, when known"),
    keep_evidence: bool = Form(True),
    principal: Principal = Depends(require_pcf_role),
):
    """A bill of materials (.xlsx, .csv or .pdf) → PcfInput-shaped lines for C02's “Import BOM”.

    Sheets are read by header name (the AI only places the material and
    quantity columns when headers can't); a PDF goes through OCR and the AI.
    Quantities are converted to the PCF units and divided by `batch_units`.
    The raw file is kept as evidence. Nothing is saved to the study: the
    person confirms the lines in the builder.
    """
    content, filename = await _read_upload(file, (".xlsx", ".csv", ".pdf"))
    override = None
    if mapping:
        try:
            override = json.loads(mapping)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="mapping must be JSON")
        if not isinstance(override, dict) or not all(v is None or isinstance(v, str) for v in override.values()):
            raise HTTPException(status_code=422, detail='mapping must look like {"name": "Material"}')
    if batch_units <= 0:
        raise HTTPException(status_code=422, detail="batch_units must be above zero")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: pcf_bom.read_bom(
                content, filename, sheet_name, override, batch_units, mass_per_unit_kg, call_llm=_llm, extract_text=_extract_text
            ),
        )
    except pcf_bom.SheetError as e:
        raise HTTPException(status_code=422, detail=str(e))
    result["evidence"] = (
        await loop.run_in_executor(None, lambda: _store_evidence(content, filename, file.content_type, study_id, principal.user_id))
        if keep_evidence
        else None
    )
    logger.info(
        "PCF BOM read by user %s: %s lines, %s with problems, ai=%s, evidence=%s",
        principal.user_id, result["total_lines"], result["lines_with_problems"], result["ai_used"], (result["evidence"] or {}).get("id"),
    )
    return pcf_bom.json_safe(result)


@router.post("/match-factors", response_model=MatchResponse)
async def match_factors(body: MatchRequest, principal: Principal = Depends(require_pcf_role)):
    """Suggest MaterialFactor ids for BOM lines, from the factors the caller may use.

    The caller sends the factor list (C04's, already scoped to its company),
    so nothing here reads another company's data. Each suggestion has a
    confidence and a reason; under 60 the line is `unmatched` and the person
    picks by hand.
    """
    lines = [l.model_dump() for l in body.lines]
    factors = [f.model_dump() for f in body.factors]
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: pcf_match.match_factors(lines, factors, body.geography_hint, call_llm=_llm)
    )
    logger.info("PCF factors matched by user %s: %s lines, %s unmatched, ai=%s", principal.user_id, result["total_lines"], result["unmatched_lines"], result["ai_used"])
    return result
