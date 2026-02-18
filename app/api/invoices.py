
import logging
from typing import Optional, List
import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Response
from pydantic import BaseModel
from app.services import storage, pipeline
from app.core import database, cloudinary_service
from app.schemas.invoice import ExtractionResponse


class BulkDeleteRequest(BaseModel):
    ids: List[int]

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/invoices/upload", response_model=ExtractionResponse)
async def upload_and_extract_invoice(
    file: UploadFile = File(...),
    site_id: Optional[int] = Form(None),
    category_id: Optional[int] = Form(None),
    uploaded_by: Optional[int] = Form(None),
    unit_names: Optional[str] = Form(None),
):
    """
    Upload an Invoice (PDF/Image), store in Cloudinary + DB, and extract structured data.
    """
    filename = file.filename or "unknown"
    logger.info(f"Received upload request for file: {filename}")

    # 1. Save upload to temp
    try:
        file_path = await storage.save_upload(file)
    except Exception as e:
        logger.error(f"Failed to save uploaded file {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    # 2. Upload to Cloudinary
    try:
        cloud_result = cloudinary_service.upload_file(file_path, folder="invoices")
    except Exception as e:
        storage.cleanup(file_path)
        logger.error(f"Cloudinary upload failed for {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {str(e)}")

    # 3. Insert into invoice table
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
        storage.cleanup(file_path)
        logger.error(f"DB insert failed for {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    # 4. Process document (OCR -> LLM -> Validate)
    try:
        available_units = [u.strip() for u in unit_names.split(",")] if unit_names else None
        result = await pipeline.process_document(
            file_path,
            filename,
            site_id=site_id,
            category_id=category_id,
            available_units=available_units,
        )

        # 5. Store OCR result in DB
        if result.data:
            database.update_invoice_ocr(invoice_id, [inv.model_dump() for inv in result.data])

        result.cloudinary_url = cloud_result["secure_url"]
        result.invoice_id = invoice_id
        return result
    except Exception as e:
        logger.error(f"Processing failed for {filename}: {e}", exc_info=True)
        storage.cleanup(file_path)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/invoices")
async def list_invoices(
    site_id: Optional[int] = Query(None),
    category_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
):
    """Get all invoices with optional filters."""
    try:
        invoices = database.get_invoices(site_id=site_id, category_id=category_id, uploaded_by=user_id)
        return invoices
    except Exception as e:
        logger.error(f"Failed to list invoices: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/invoices/bulk")
async def bulk_delete_invoices(body: BulkDeleteRequest):
    """Bulk delete invoices (DB + Cloudinary)."""
    ids = body.ids
    if not ids:
        raise HTTPException(status_code=400, detail="ids list is required")

    deleted = database.bulk_delete_invoices(ids)

    # Cleanup Cloudinary
    for inv in deleted:
        cloudinary_service.delete_file(inv["cloudinary_public_id"])

    return {
        "message": f"Successfully deleted {len(deleted)} invoice(s)",
        "deleted": len(deleted),
    }


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: int):
    """Get a single invoice by ID."""
    invoice = database.get_invoice_by_id(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.delete("/invoices/{invoice_id}")
async def delete_invoice(invoice_id: int):
    """Delete an invoice (DB + Cloudinary)."""
    deleted = database.delete_invoice(invoice_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Cleanup Cloudinary
    cloudinary_service.delete_file(deleted["cloudinary_public_id"])

    return {"message": "Invoice deleted successfully", "invoice_id": invoice_id}


@router.get("/invoices/{invoice_id}/serve")
async def serve_invoice_file(invoice_id: int):
    """Proxy-serve an invoice file from Cloudinary, bypassing any access restrictions."""
    record = database.get_invoice_by_id(invoice_id)
    if not record:
        raise HTTPException(status_code=404, detail="Invoice not found")

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
        record = database.get_invoice_by_id(invoice_id)
        if not record:
            raise HTTPException(status_code=404, detail="Invoice not found")
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
        storage.cleanup(file_path)
        raise HTTPException(status_code=500, detail=str(e))
