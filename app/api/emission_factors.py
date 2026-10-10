import asyncio
import json
import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.services import storage
from app.services.excel_parser import parse_emission_factor_excel, infer_category_mapping
from app.services import storage as storage_service
from app.schemas.emission_factor import (
    ParseExcelResponse,
    DbCategory,
    EmissionFactorUploadRecord,
    UpdateUploadResultsRequest,
)
from app.core import database, cloudinary_service
from app.core.auth import Principal, require_superadmin

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
    sheet_name: Optional[str] = Form(None),
    principal: Principal = Depends(require_superadmin),
):
    """
    Upload an emission factor Excel file and get structured, editable preview data.

    Uses a two-pass approach:
    - Pass 1: LLM detects spreadsheet structure from first ~20 rows
    - Pass 2: openpyxl deterministically extracts all data rows

    The file is persisted to Cloudinary and an upload record is created in the DB.
    Optionally accepts `db_categories` (JSON string) to auto-infer
    category mappings via LLM in the same request.
    Optionally accepts `sheet_name` to parse a specific sheet instead of the first.
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
            lambda: parse_emission_factor_excel(file_path, filename, sheet_name=sheet_name),
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
                # Blocking LLM call: keep it off the event loop.
                result.category_suggestions = await run_in_threadpool(
                    infer_category_mapping, result.parent_categories, cats
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
                    uploaded_by=principal.user_id,  # never the client's value
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
# Re-analyze: change sheet or override column mapping without re-uploading
# ---------------------------------------------------------------------------

class ReAnalyzeRequest(BaseModel):
    upload_id: int
    sheet_name: Optional[str] = None
    schema_override: Optional[dict] = None
    db_categories: Optional[list[dict]] = None


@router.post(
    "/emission-factors/re-analyze",
    response_model=ParseExcelResponse,
)
async def re_analyze_emission_factor_file(body: ReAnalyzeRequest):
    """
    Re-parse a previously uploaded emission factor file with a different sheet
    or column mapping. Downloads the file from Cloudinary using the upload record,
    so the user doesn't need to re-upload.
    """
    # Look up the upload record to get the Cloudinary URL
    try:
        uploads = database.get_emission_factor_uploads()
        upload_row = next(
            (u for u in uploads if u["id"] == body.upload_id), None
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to look up upload: {e}")

    if not upload_row:
        raise HTTPException(status_code=404, detail="Upload record not found")

    cloudinary_url = upload_row.get("cloudinary_url")
    if not cloudinary_url:
        raise HTTPException(
            status_code=422,
            detail="No file URL found for this upload. Please re-upload the file.",
        )

    filename = upload_row.get("file_name", "unknown.xlsx")
    file_path = None
    try:
        file_path = await storage_service.download_from_url(cloudinary_url, filename)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: parse_emission_factor_excel(
                file_path,
                filename,
                sheet_name=body.sheet_name,
                schema_override=body.schema_override,
            ),
        )

        if not result.factors:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No emission factor data could be extracted from this sheet. "
                    "Please try a different sheet or check the column mapping."
                ),
            )

        # Infer category mapping if DB categories were provided
        if body.db_categories and result.parent_categories:
            try:
                cats = [DbCategory(**c) for c in body.db_categories]
                # Blocking LLM call: keep it off the event loop.
                result.category_suggestions = await run_in_threadpool(
                    infer_category_mapping, result.parent_categories, cats
                )
            except Exception as e:
                logger.warning(f"Category inference failed (non-fatal): {e}")

        result.upload_id = body.upload_id
        result.cloudinary_url = cloudinary_url

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Re-analyze failed for upload {body.upload_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Re-analyze failed: {str(e)}")
    finally:
        if file_path:
            storage_service.cleanup(file_path)


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
                database.release_connection(conn)

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
