
import logging
from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
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
        result = await pipeline.process_document(
            file_path, 
            filename, 
            site_id=site_id, 
            category_id=category_id
        )

        # 5. Store OCR result in DB
        if result.data:
            database.update_invoice_ocr(invoice_id, [inv.model_dump() for inv in result.data])

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



# Keep the old extract endpoint for backward compatibility
@router.post("/extract", response_model=ExtractionResponse)
async def extract_invoice(
    file: UploadFile = File(...),
):
    """
    Legacy: Upload an Invoice and extract data without storing.
    Use POST /invoices/upload for the full flow.
    """
    filename = file.filename or "unknown"
    logger.info(f"Received extract request for file: {filename}")

    try:
        file_path = await storage.save_upload(file)
    except Exception as e:
        logger.error(f"Failed to save uploaded file {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    try:
        result = await pipeline.process_document(file_path, filename)
        return result
    except Exception as e:
        logger.error(f"Processing failed for {filename}: {e}", exc_info=True)
        storage.cleanup(file_path)
        raise HTTPException(status_code=500, detail=str(e))
