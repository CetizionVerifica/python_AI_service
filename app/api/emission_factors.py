import asyncio
import json
import logging
from typing import Optional, List

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from pydantic import BaseModel

from app.services import storage
from app.services.excel_parser import parse_emission_factor_excel, infer_category_mapping
from app.schemas.emission_factor import (
    ParseExcelResponse,
    DbCategory,
    EmissionFactorUploadRecord,
    UpdateUploadResultsRequest,
)
from app.core import database, cloudinary_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/emission-factors/parse-excel",
    response_model=ParseExcelResponse,
)
async def parse_emission_factor_file(
    file: UploadFile = File(...),
    db_categories: Optional[str] = Form(None),
    uploaded_by: Optional[int] = Form(None),
):
    """
    Upload an emission factor Excel file and get structured, editable preview data.

    Uses a two-pass approach:
    - Pass 1: LLM detects spreadsheet structure from first ~20 rows
    - Pass 2: openpyxl deterministically extracts all data rows

    The file is persisted to Cloudinary and an upload record is created in the DB.
    Optionally accepts `db_categories` (JSON string) to auto-infer
    category mappings via LLM in the same request.
    """
    filename = file.filename or "unknown.xlsx"

    if not filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx and .xls files are supported",
        )

    file_path = None
    try:
        file_path = await storage.save_upload(file)

        # Run Cloudinary upload and Excel parsing concurrently
        loop = asyncio.get_event_loop()
        cloud_task = loop.run_in_executor(
            None,
            lambda: cloudinary_service.upload_file(file_path, folder="emission_factor_uploads"),
        )
        # parse is sync+CPU-bound — also run in executor to not block
        parse_task = loop.run_in_executor(
            None,
            lambda: parse_emission_factor_excel(file_path, filename),
        )

        raw = await asyncio.gather(cloud_task, parse_task, return_exceptions=True)
        cloud_result, result = raw[0], raw[1]

        if isinstance(cloud_result, Exception):
            logger.error(f"Cloudinary upload failed for {filename}: {cloud_result}")
            # Non-fatal: continue without persistence
            cloud_result = None

        if isinstance(result, Exception):
            logger.error(f"Excel parsing failed for {filename}: {result}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Failed to parse file: {str(result)}"
            )

        if not result.factors:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No emission factor data could be extracted from this file. "
                    "Please check the file format."
                ),
            )

        # Infer category mapping if DB categories were provided
        if db_categories and result.parent_categories:
            try:
                cats = [DbCategory(**c) for c in json.loads(db_categories)]
                result.category_suggestions = infer_category_mapping(
                    result.parent_categories, cats
                )
            except Exception as e:
                logger.warning(
                    f"Category inference failed (non-fatal): {e}"
                )

        # Persist upload record in DB
        if cloud_result:
            try:
                upload_row = database.insert_emission_factor_upload(
                    file_name=filename,
                    cloudinary_url=cloud_result["secure_url"],
                    cloudinary_public_id=cloud_result["public_id"],
                    file_size=cloud_result.get("bytes"),
                    uploaded_by=uploaded_by,
                    layout_type=result.schema_detected.layout_type,
                    total_records=result.total_records,
                )
                result.upload_id = upload_row["id"]
                result.cloudinary_url = cloud_result["secure_url"]
            except Exception as e:
                logger.warning(f"DB upload record insert failed (non-fatal): {e}")

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Excel parsing failed for {filename}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to parse file: {str(e)}"
        )
    finally:
        if file_path:
            storage.cleanup(file_path)


# ---------------------------------------------------------------------------
# Upload history endpoints
# ---------------------------------------------------------------------------

@router.get("/emission-factors/uploads")
async def list_emission_factor_uploads(
    uploaded_by: Optional[int] = Query(None),
):
    """List all emission factor upload records, newest first."""
    try:
        uploads = database.get_emission_factor_uploads(uploaded_by=uploaded_by)
        return uploads
    except Exception as e:
        logger.error(f"Failed to list emission factor uploads: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/emission-factors/uploads/{upload_id}")
async def update_upload_results(upload_id: int, body: UpdateUploadResultsRequest):
    """Update an upload record with final results after bulk create."""
    try:
        # Optionally update site_id and category_ids
        if body.site_id is not None or body.category_ids is not None:
            conn = database.get_connection()
            try:
                with conn.cursor() as cur:
                    parts = []
                    params = []
                    if body.site_id is not None:
                        parts.append("site_id = %s")
                        params.append(body.site_id)
                    if body.category_ids is not None:
                        parts.append("category_ids = %s")
                        params.append(body.category_ids)
                    if parts:
                        params.append(upload_id)
                        cur.execute(
                            f"UPDATE emission_factor_uploads SET {', '.join(parts)} WHERE id = %s",
                            params,
                        )
                        conn.commit()
            finally:
                conn.close()

        updated = database.update_emission_factor_upload_results(
            upload_id=upload_id,
            records_created=body.records_created,
            records_skipped=body.records_skipped,
            status=body.status,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Upload record not found")
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update upload {upload_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class BulkDeleteUploadsRequest(BaseModel):
    ids: list[int]


@router.delete("/emission-factors/uploads/bulk")
async def bulk_delete_emission_factor_uploads(body: BulkDeleteUploadsRequest):
    """Bulk delete upload records and their Cloudinary files."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="ids list is required")

    deleted = database.bulk_delete_emission_factor_uploads(body.ids)

    # Cleanup Cloudinary for each deleted record
    for record in deleted:
        cloudinary_service.delete_file(record["cloudinary_public_id"])

    return {
        "message": f"Successfully deleted {len(deleted)} upload(s)",
        "deleted": len(deleted),
    }


@router.delete("/emission-factors/uploads/{upload_id}")
async def delete_emission_factor_upload(upload_id: int):
    """Delete an upload record and its Cloudinary file."""
    deleted = database.delete_emission_factor_upload(upload_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Upload record not found")

    # Cleanup Cloudinary
    cloudinary_service.delete_file(deleted["cloudinary_public_id"])

    return {"message": "Upload record deleted", "id": upload_id}
