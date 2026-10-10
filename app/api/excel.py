
import asyncio
import logging
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from app.services.excel_parser import (
    get_unique_categories,
    get_preview_rows,
    import_all_rows,
    read_headers_from_bytes,
)
from app.core.database import ensure_uploaded_documents_table, insert_uploaded_document, get_uploaded_document_by_id
from app.core.auth import Principal, require_user
from app.core import cloudinary_service
from app.services import storage
from app.core.config import settings

import pandas as pd
import io

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/excel", tags=["excel"])


def _own_document(document_id, principal: Principal) -> int:
    """The document id, or 404 unless the caller uploaded it (Superadmin: any)."""
    try:
        document_id = int(document_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="document_id must be a whole number")
    doc = get_uploaded_document_by_id(document_id)
    if not doc or not (principal.is_superadmin or doc.get("uploaded_by") == principal.user_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return document_id

@router.post("/upload")
async def upload_excel(file: UploadFile = File(...), principal: Principal = Depends(require_user)):
    allowed = {"xlsx", "xls", "csv"}

    filename = file.filename or ""
    if "." not in filename:
        raise HTTPException(status_code=400, detail="File must have an extension (.xlsx, .xls, .csv).")

    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, .csv files are allowed.")

    local_path = None
    uploaded_to_cloud = False
    inserted = False

    try:
        contents = await file.read()

        # enforce 100 MB limit
        if len(contents) > 100 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File size exceeds 100 MB limit.")

        # Parse headers using the same detection the import path uses, so the
        # columns offered on the mapping screen are exactly the ones that will
        # be read back later.
        headers = read_headers_from_bytes(contents, ext)

        if not headers:
            raise ValueError("The uploaded file appears to be empty or has no columns.")

        # Write the bytes locally only so Cloudinary's chunked uploader can
        # stream them. This copy is transient: it is removed in the `finally`
        # below and nothing after this request may depend on it.
        file_id = str(uuid.uuid4())
        local_path = Path(settings.TEMP_DIR) / f"{file_id}.{ext}"
        with open(local_path, "wb") as f:
            f.write(contents)

        # The remaining wizard steps (unique-categories / preview / import) each
        # re-read this document, possibly minutes later and possibly on another
        # replica. Container-local disk is neither shared nor durable, so hand
        # the file to shared storage and record that location instead.
        stored_url = str(local_path)
        public_id = None

        if cloudinary_service.is_configured():
            try:
                loop = asyncio.get_running_loop()
                cloud = await loop.run_in_executor(
                    None,
                    lambda: cloudinary_service.upload_file(local_path, folder="excel-imports"),
                )
                stored_url = cloud["secure_url"]
                public_id = cloud["public_id"]
                uploaded_to_cloud = True
            except Exception:
                # Degrade to the previous instance-local behaviour rather than
                # rejecting the upload outright.
                logger.warning(
                    f"Cloudinary upload failed for {filename}; storing local temp path instead",
                    exc_info=True,
                )
        else:
            logger.warning(
                "Cloudinary is not configured; storing local temp path. "
                "This document will not survive a restart or reach other instances."
            )

        try:
            doc = insert_uploaded_document(
                document_name=filename,
                cloudinary_url=stored_url,
                cloudinary_public_id=public_id,
                public_url=stored_url,
                file_type=file.content_type,
                file_size=len(contents),
                uploaded_by=principal.user_id,
            )
        except Exception:
            # No row will ever point at the uploaded asset, and the cleanup job
            # only walks uploaded_documents — drop it now or it leaks forever.
            if public_id:
                try:
                    cloudinary_service.delete_file(public_id)
                except Exception:
                    logger.warning(
                        f"Orphaned Cloudinary asset {public_id}: upload succeeded but the "
                        "document row failed and the asset could not be deleted",
                        exc_info=True,
                    )
            raise
        inserted = True

        return {"document_id": int(doc["id"]), "headers": headers}

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"excel upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process file.")
    finally:
        # Drop the local copy once shared storage holds it, or when no document
        # row ended up pointing at it. On the fallback path the row *is* the
        # only reference, so the file has to stay.
        if local_path is not None and (uploaded_to_cloud or not inserted):
            storage.cleanup(local_path)

# ---- Step 2: unique categories BEFORE preview ----
@router.post("/unique-categories")
def unique_categories(payload: dict, principal: Principal = Depends(require_user)):
    document_id = _own_document(payload.get("document_id"), principal)
    try:
        mappings = payload.get("mappings") or {}
        cats, total = get_unique_categories(document_id, mappings)
        return {"unique_categories": cats, "total_rows": total}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"unique-categories failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch unique categories.")



@router.post("/preview")
def preview(payload: dict, principal: Principal = Depends(require_user)):
    # site_id is checked against the caller's sites by enforce_site_scope.
    document_id = _own_document(payload.get("document_id"), principal)
    try:
        mappings = payload.get("mappings") or {}
        selected_categories = payload.get("selected_categories") or []
        page = int(payload.get("page", 1))
        page_size = int(payload.get("page_size", 100))

        site_id = int(payload.get("site_id"))
        category_id = int(payload.get("category_id"))
        date_of_reporting = str(payload.get("date_of_reporting"))

        rows, total = get_preview_rows(
            document_id=document_id,
            mappings=mappings,
            selected_categories=selected_categories,
            page=page,
            page_size=page_size,
            site_id=site_id,
            category_id=category_id,
            date_of_reporting=date_of_reporting,
        )
        return {"rows": rows, "total_rows": total, "page": page, "page_size": page_size}

    except HTTPException:
        raise
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"preview failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch preview.")

# ---- Step 4: import all + calculate + save ----
@router.post("/import")
def bulk_import(payload: dict, principal: Principal = Depends(require_user)):
    # site_id is checked against the caller's sites by enforce_site_scope.
    document_id = _own_document(payload.get("document_id"), principal)
    try:
        mappings = payload.get("mappings") or {}
        selected_categories = payload.get("selected_categories") or []
        site_id = int(payload.get("site_id"))
        category_id = int(payload.get("category_id"))
        date_of_reporting = str(payload.get("date_of_reporting"))
        # Rows are created by whoever signed in, never by a client-sent user_id.
        user_id = principal.user_id

        res = import_all_rows(
            document_id=document_id,
            mappings=mappings,
            selected_categories=selected_categories,
            site_id=site_id,
            category_id=category_id,
            date_of_reporting=date_of_reporting,
            chunk_size=2000,  # fast
            user_id=user_id,
        )

        # import_all_rows deletes the stored file as soon as the rows commit.
        return res
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"import failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Import failed.")