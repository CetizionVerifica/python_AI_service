"""KPI-12 · KPI AI assist routes (layer file app/kpi/CLAUDE.md).

* ``POST /v1/kpi/suggest-values`` and ``POST /v1/kpi/suggest-mapping``:
  ESG-lite's service key only (``X-Service-Key``), from its
  ``POST /kpi/ai/suggest-values`` and ``/kpi/ai/suggest-mapping``; a
  signed-in user's token gets 403. Suggest only: nothing is stored.
* ``POST /v1/kpi/documents``: a signed-in user stores the file the
  "Fill from a document" drop zone receives, in ``uploaded_documents``, and
  gets the ``document_id`` ESG-lite then passes to suggest-values. The
  document belongs to the uploader's company (app/kpi/documents.py).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.core import cloudinary_service
from app.core.auth import Principal, allow_service, require_user
from app.core.config import settings
from app.core.database import insert_uploaded_document
from app.kpi import documents, mapping, values
from app.kpi.schemas import (
    SuggestMappingRequest,
    SuggestMappingResponse,
    SuggestValuesRequest,
    SuggestValuesResponse,
)
from app.services import storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kpi", tags=["KPI"])

AI_UNAVAILABLE = "AI not available right now"


def service_only(principal: Principal = Depends(allow_service)) -> Principal:
    if principal.kind != "service":
        raise HTTPException(status_code=403, detail="ESG-lite only")
    return principal


@router.post("/suggest-values", response_model=SuggestValuesResponse, dependencies=[Depends(service_only)])
def suggest_values(payload: SuggestValuesRequest) -> SuggestValuesResponse:
    doc = documents.locate(payload.document_id, payload.company_id, payload.user_id)
    pages = documents.read_pages(doc, documents.fetch_bytes(doc))
    try:
        return values.suggest(payload, doc, pages)
    except values.AiUnavailable:
        raise HTTPException(status_code=503, detail=AI_UNAVAILABLE)


@router.post("/suggest-mapping", response_model=SuggestMappingResponse, dependencies=[Depends(service_only)])
def suggest_mapping(payload: SuggestMappingRequest) -> SuggestMappingResponse:
    return mapping.suggest(payload)


@router.post("/documents")
async def upload_document(file: UploadFile = File(...), principal: Principal = Depends(require_user)):
    if principal.is_superadmin:
        # A Superadmin belongs to no company, so suggest-values could never read the file.
        raise HTTPException(status_code=403, detail="Upload the document as a user of the client's company")
    filename = (file.filename or "").strip()
    ext = documents.ext_of(filename)
    if ext not in documents.SUPPORTED_EXTS:
        raise HTTPException(status_code=400, detail="Upload a PDF, an image, an .xlsx or a .csv file")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="The file is empty")
    if len(contents) > settings.MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"The file is larger than {settings.MAX_FILE_MB} MB")

    local_path = Path(settings.TEMP_DIR) / f"{uuid.uuid4()}.{ext}"
    local_path.write_bytes(contents)
    stored_url, public_id = str(local_path), None
    try:
        if cloudinary_service.is_configured():
            try:
                loop = asyncio.get_running_loop()
                cloud = await loop.run_in_executor(None, lambda: cloudinary_service.upload_file(local_path, folder="kpi-documents"))
                stored_url, public_id = cloud["secure_url"], cloud["public_id"]
            except Exception:
                logger.warning("kpi document: Cloudinary upload failed; keeping the local temp path", exc_info=True)
        try:
            doc = insert_uploaded_document(
                document_name=filename[:255],
                cloudinary_url=stored_url,
                cloudinary_public_id=public_id,
                public_url=stored_url,
                file_type=file.content_type,
                file_size=len(contents),
                uploaded_by=principal.user_id,
            )
        except Exception:
            if public_id:
                try:
                    cloudinary_service.delete_file(public_id)
                except Exception:
                    logger.warning(f"Orphaned Cloudinary asset {public_id}", exc_info=True)
            else:
                storage.cleanup(local_path)
            raise HTTPException(status_code=500, detail="Could not store the document")
    finally:
        # The local copy stays only when it is the stored location (no Cloudinary).
        if public_id:
            storage.cleanup(local_path)
    logger.info(f"kpi document {doc['id']} stored by user {principal.user_id}")
    return {"document_id": int(doc["id"]), "document_name": doc["document_name"]}
