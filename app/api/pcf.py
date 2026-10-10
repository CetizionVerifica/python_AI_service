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
from app.schemas.pcf import FactorSheetResponse
from app.services import pcf_sheet

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
