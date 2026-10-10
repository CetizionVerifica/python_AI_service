
import asyncio
import logging
from typing import Optional, List
import httpx
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query, Response
from pydantic import BaseModel
from app.services import storage, pipeline
from app.core import database, cloudinary_service
from app.core.auth import Principal, require_user
from app.schemas.invoice import ExtractionResponse


class BulkDeleteRequest(BaseModel):
    ids: List[int]

logger = logging.getLogger(__name__)

router = APIRouter()

_NOT_FOUND = HTTPException(status_code=404, detail="Invoice not found")


def _visible_invoice(invoice_id: int, principal: Principal) -> dict:
    """The invoice, or 404 when it is missing or outside the caller's sites."""
    record = database.get_invoice_by_id(invoice_id)
    if not record or not principal.can_access_record(record.get("site_id"), record.get("uploaded_by")):
        raise _NOT_FOUND
    return record


@router.post("/invoices/upload", response_model=ExtractionResponse)
async def upload_and_extract_invoice(
    file: UploadFile = File(...),
    site_id: Optional[int] = Form(None),
    category_id: Optional[int] = Form(None),
    uploaded_by: Optional[int] = Form(None),
    unit_names: Optional[str] = Form(None),
    principal: Principal = Depends(require_user),
):
    """
    Upload an Invoice (PDF/Image), store in Cloudinary + DB, and extract structured data.
    Cloudinary upload and the OCR/LLM pipeline run concurrently to minimise latency.
    """
    # The uploader is whoever signed in, never what the client claims.
    uploaded_by = principal.user_id
    filename = file.filename or "unknown"
    logger.info(f"Received upload request for file: {filename}")
    file_path = None

    try:
        # 1. Save upload to temp
        try:
            file_path = await storage.save_upload(file)
        except Exception as e:
            logger.error(f"Failed to save uploaded file {filename}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

        # 2. Run Cloudinary upload and OCR pipeline concurrently.
        #    cloudinary_service.upload_file is synchronous, so wrap it in an executor
        #    so it doesn't block the event loop and can truly overlap with the pipeline.
        available_units = [u.strip() for u in unit_names.split(",")] if unit_names else None
        loop = asyncio.get_event_loop()

        cloud_task = loop.run_in_executor(
            None,
            lambda: cloudinary_service.upload_file(file_path, folder="invoices"),
        )
        pipeline_task = pipeline.process_document(
            file_path,
            filename,
            site_id=site_id,
            category_id=category_id,
            available_units=available_units,
        )

        raw = await asyncio.gather(cloud_task, pipeline_task, return_exceptions=True)
        cloud_result, result = raw[0], raw[1]

        if isinstance(cloud_result, Exception):
            logger.error(f"Cloudinary upload failed for {filename}: {cloud_result}")
            raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {cloud_result}")
        if isinstance(result, Exception):
            logger.error(f"Processing failed for {filename}: {result}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(result))

        # 3. Insert into invoice table (both results available now)
        try:
            invoice_row = database.insert_invoice(
                file_name=filename,
                cloudinary_url=cloud_result["secure_url"],
                cloudinary_public_id=cloud_result["public_id"],
                file_type=file.content_type or "application/octet-stream",
                file_size=cloud_result.get("bytes"),
                uploaded_by=uploaded_by,
                site_id=site_id,
                category_id=category_id,
            )
            invoice_id = invoice_row["invoice_id"]
            logger.info(f"Invoice record created with ID: {invoice_id}")
        except Exception as e:
            logger.error(f"DB insert failed for {filename}: {e}")
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        # 4. Store OCR result in DB
        if result.data:
            database.update_invoice_ocr(invoice_id, [inv.model_dump() for inv in result.data])

        result.cloudinary_url = cloud_result["secure_url"]
        result.invoice_id = invoice_id
        return result

    finally:
        # Always clean up the temp file — must happen after both concurrent tasks finish
        if file_path:
            storage.cleanup(file_path)


@router.get("/invoices")
async def list_invoices(
    site_id: Optional[int] = Query(None),
    category_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    principal: Principal = Depends(require_user),
):
    """Get the caller's invoices (their sites; Superadmin: all) with optional filters."""
    try:
        invoices = database.get_invoices(
            site_id=site_id,
            category_id=category_id,
            uploaded_by=user_id,
            scope_site_ids=None if principal.site_ids is None else sorted(principal.site_ids),
            scope_user_id=principal.user_id,
        )
        return invoices
    except Exception as e:
        logger.error(f"Failed to list invoices: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/invoices/bulk")
async def bulk_delete_invoices(body: BulkDeleteRequest, principal: Principal = Depends(require_user)):
    """Bulk delete invoices (DB + Cloudinary)."""
    ids = body.ids
    if not ids:
        raise HTTPException(status_code=400, detail="ids list is required")
    # All or nothing: one invoice outside the caller's sites fails the request.
    for invoice_id in set(ids):
        _visible_invoice(invoice_id, principal)

    # The link check happens inside the delete transaction (rows locked).
    deleted = database.bulk_delete_invoices(ids)

    # Cleanup Cloudinary, except files that ESG-lite evidence documents still
    # use (ESG-lite deletes the file when the last such document goes).
    for inv in deleted:
        if inv["file_linked"]:
            continue
        cloudinary_service.delete_file(inv["cloudinary_public_id"])

    return {
        "message": f"Successfully deleted {len(deleted)} invoice(s)",
        "deleted": len(deleted),
        "files_kept": sum(1 for inv in deleted if inv["file_linked"]),
    }


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: int, principal: Principal = Depends(require_user)):
    """Get a single invoice by ID."""
    return _visible_invoice(invoice_id, principal)


@router.delete("/invoices/{invoice_id}")
async def delete_invoice(invoice_id: int, principal: Principal = Depends(require_user)):
    """Delete an invoice (DB + Cloudinary)."""
    _visible_invoice(invoice_id, principal)
    # The link check happens inside the delete transaction (row locked).
    deleted = database.delete_invoice(invoice_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Cleanup Cloudinary, unless ESG-lite evidence documents still use the file
    # (ESG-lite deletes it when the last such document goes).
    file_kept = deleted["file_linked"]
    if not file_kept:
        cloudinary_service.delete_file(deleted["cloudinary_public_id"])

    return {"message": "Invoice deleted successfully", "invoice_id": invoice_id, "file_kept": file_kept}


@router.get("/invoices/{invoice_id}/serve")
async def serve_invoice_file(invoice_id: int, principal: Principal = Depends(require_user)):
    """Proxy-serve an invoice file from Cloudinary to a caller who may see it."""
    record = _visible_invoice(invoice_id, principal)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(record["cloudinary_url"])
            # If direct URL is restricted, try a signed delivery URL via SDK
            if resp.status_code == 401:
                import cloudinary.utils
                signed_url, _ = cloudinary.utils.cloudinary_url(
                    record["cloudinary_public_id"],
                    resource_type="raw",
                    type="upload",
                    sign_url=True,
                )
                resp = await client.get(signed_url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"Failed to fetch invoice {invoice_id} from Cloudinary: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch invoice file from storage")
    except Exception as e:
        logger.error(f"Error serving invoice {invoice_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    content_type = record.get("file_type") or "application/octet-stream"
    filename = record.get("file_name", "invoice")
    return Response(
        content=resp.content,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# Legacy extract endpoint — accepts either a file upload OR an invoice_id
# (to re-run extraction against an already-stored Cloudinary document)
@router.post("/extract", response_model=ExtractionResponse)
async def extract_invoice(
    file: Optional[UploadFile] = File(None),
    invoice_id: Optional[int] = Form(None),
    site_id: Optional[int] = Form(None),
    category_id: Optional[int] = Form(None),
    unit_names: Optional[str] = Form(None),
    principal: Principal = Depends(require_user),
):
    """
    Extract structured data from an invoice.
    - Pass `file` to extract from a new upload (not stored).
    - Pass `invoice_id` to re-extract from a previously uploaded invoice via Cloudinary.
    """
    if file is None and invoice_id is None:
        raise HTTPException(status_code=400, detail="Either file or invoice_id must be provided")

    if invoice_id is not None:
        # Reuse path: look up record, download from Cloudinary, run pipeline
        record = _visible_invoice(invoice_id, principal)
        try:
            file_path = await storage.download_from_url(record["cloudinary_url"], record["file_name"])
        except Exception as e:
            logger.error(f"Failed to download invoice {invoice_id} from Cloudinary: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to download invoice from storage: {e}")
        filename = record["file_name"]
        cloudinary_url = record["cloudinary_url"]
        effective_site_id = site_id if site_id is not None else record["site_id"]
        effective_category_id = category_id if category_id is not None else record["category_id"]
    else:
        # Original path: use uploaded file
        filename = file.filename or "unknown"
        logger.info(f"Received extract request for file: {filename}")
        try:
            file_path = await storage.save_upload(file)
        except Exception as e:
            logger.error(f"Failed to save uploaded file {filename}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
        cloudinary_url = None
        effective_site_id = site_id
        effective_category_id = category_id

    try:
        available_units = [u.strip() for u in unit_names.split(",")] if unit_names else None
        result = await pipeline.process_document(
            file_path, filename,
            site_id=effective_site_id,
            category_id=effective_category_id,
            available_units=available_units,
        )
        result.cloudinary_url = cloudinary_url
        return result
    except Exception as e:
        logger.error(f"Processing failed for {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        storage.cleanup(file_path)
