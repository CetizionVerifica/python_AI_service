
import logging
from fastapi import APIRouter, UploadFile, File, HTTPException
from app.core.cloudinary_service import upload_file
from app.services.storage import save_temp_file_from_bytes
from app.services.excel_parser import (
    get_unique_categories,
    get_preview_rows,
    import_all_rows,
)
from app.core.database import ensure_uploaded_documents_table, insert_uploaded_document

import pandas as pd
import io

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/excel", tags=["excel"])


from fastapi import UploadFile, File, HTTPException
import os
import io
import pandas as pd

@router.post("/upload")
async def upload_excel(file: UploadFile = File(...)):
    allowed = {"xlsx", "xls", "csv"}

    filename = file.filename or ""
    if "." not in filename:
        raise HTTPException(status_code=400, detail="File must have an extension (.xlsx, .xls, .csv).")

    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, .csv files are allowed.")

    temp_path = None
    try:
        contents = await file.read()

        # parse headers — try row 0, 1, 2 (same as excel_parser)
        headers: list[str] = []
        for header_row in (0, 1, 2):
            try:
                if ext == "csv":
                    df = pd.read_csv(io.BytesIO(contents), dtype=str, nrows=5, header=header_row)
                else:
                    df = pd.read_excel(io.BytesIO(contents), dtype=str, nrows=5, header=header_row)

                cols = [c for c in df.columns if not str(c).startswith("Unnamed:")]
                if cols:
                    headers = [str(c).strip() for c in cols]
                    break
            except Exception:
                continue

        if not headers:
            raise ValueError("The uploaded file appears to be empty or has no columns.")

        # save temp file (needed for cloudinary uploader)
        temp_path = await save_temp_file_from_bytes(contents, filename)

        # upload to cloudinary
        cloudinary_result = upload_file(temp_path, folder="excel-uploads")
        cloud_url = cloudinary_result["secure_url"]
        public_id = cloudinary_result.get("public_id")  # optional but useful

        doc = insert_uploaded_document(
            document_name=filename,
            cloudinary_url=cloud_url,
            cloudinary_public_id=public_id,
            public_url=cloud_url,
            file_type=file.content_type,
            file_size=len(contents),
        )

        return {"document_id": int(doc["id"]), "headers": headers}

    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"excel upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process file.")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                logger.warning(f"Failed to delete temp file: {temp_path}", exc_info=True)

# ---- Step 2: unique categories BEFORE preview ----
@router.post("/unique-categories")
async def unique_categories(payload: dict):
    try:
        document_id = int(payload.get("document_id"))
        mappings = payload.get("mappings") or {}
        cats, total = get_unique_categories(document_id, mappings)
        return {"unique_categories": cats, "total_rows": total}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"unique-categories failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch unique categories.")



@router.post("/preview")
async def preview(payload: dict):
    try:
        document_id = int(payload.get("document_id"))
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

    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"preview failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch preview.")

# ---- Step 4: import all + calculate + save ----
@router.post("/import")
async def bulk_import(payload: dict):
    try:
        document_id = int(payload.get("document_id"))
        mappings = payload.get("mappings") or {}
        selected_categories = payload.get("selected_categories") or []
        site_id = int(payload.get("site_id"))
        category_id = int(payload.get("category_id"))
        date_of_reporting = str(payload.get("date_of_reporting"))

        res = import_all_rows(
            document_id=document_id,
            mappings=mappings,
            selected_categories=selected_categories,
            site_id=site_id,
            category_id=category_id,
            date_of_reporting=date_of_reporting,
            chunk_size=2000,  # fast
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"import failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Import failed.")