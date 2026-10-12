
import asyncio
import logging
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Request
from app.services.excel_parser import (
    delete_document_file,
    get_unique_categories,
    load_mapped_sheet,
    read_headers_from_bytes,
)
from app.services import excel_plan_client as plans
from app.core.database import claim_uploaded_document_for_import, company_id_for_site, release_import_claim
from app.core.database import insert_uploaded_document, get_uploaded_document_by_id
from app.core.auth import Principal, require_user
from app.core import cloudinary_service
from app.services import storage
from app.core.config import settings


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



def _bearer(request: Request) -> str:
    """The caller's own sign-in token, forwarded to ESG-lite so its scoping applies (never the service key)."""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Sign in again to import rows.")
    return token.strip()


def _esg_lite_http_error(e: plans.EsgLiteError) -> HTTPException:
    # ESG-lite's own status and IM0 body travel on unchanged (never a stack, never SQL).
    return HTTPException(status_code=e.status, detail=e.body())


def _sheet_rows(payload: dict) -> tuple[plans.SheetRows, dict]:
    """The sheet's rows as ImportRows for the site, category and month in the payload."""
    site_id = int(payload.get("site_id"))
    category_id = int(payload.get("category_id"))
    date_of_reporting = str(payload.get("date_of_reporting"))
    document_id = int(payload["document_id"])
    mapped_df, document_name = load_mapped_sheet(document_id, payload.get("mappings") or {})
    sheet = plans.rows_from_frame(mapped_df, payload.get("selected_categories") or [], site_id, category_id, date_of_reporting)
    return sheet, {"site_id": site_id, "category_id": category_id, "source_name": document_name or f"upload-{document_id}"}


@router.post("/preview")
def preview(payload: dict, request: Request, principal: Principal = Depends(require_user)):
    """Plan the sheet on ESG-lite and answer with the plan and a page of its rows.

    Since IM11 the calculation is ESG-lite's: the rows go to POST /imports/plans
    as ``bulk_upload`` and come back planned (factor, total, FERA twin, issues).
    With ``plan_id`` the call only pages that plan (``page``, ``page_size``,
    ``state``); ``cancel_plan_id`` frees the plan of an earlier preview.
    """
    # site_id is checked against the caller's sites by enforce_site_scope, and again by ESG-lite.
    document_id = _own_document(payload.get("document_id"), principal)
    payload = {**payload, "document_id": document_id}
    try:
        page = int(payload.get("page", 1))
        page_size = int(payload.get("page_size", 100))
        state = payload.get("state") or None
        sheet, ctx = _sheet_rows(payload)
        with plans.PlanClient(_bearer(request)) as client:
            plan_id = payload.get("plan_id")
            if plan_id:
                return plans.plan_page(client, str(plan_id), page=page, page_size=page_size, state=state, local_issues=sheet.local_issues, not_selected=sheet.not_selected, sheet_rows=sheet.total_rows)
            company_id = company_id_for_site(ctx["site_id"]) if principal.is_superadmin else None
            return plans.plan_sheet(
                client,
                sheet=sheet,
                source_name=ctx["source_name"],
                company_id=company_id,
                page=page,
                page_size=page_size,
                state=state,
                cancel_plan_id=payload.get("cancel_plan_id") or None,
            )
    except HTTPException:
        raise
    except plans.EsgLiteError as e:
        raise _esg_lite_http_error(e)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"preview failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch preview.")


# ---- Step 4: commit the plan ----
@router.post("/import")
def bulk_import(payload: dict, request: Request, principal: Principal = Depends(require_user)):
    """Commit the previewed plan (``plan_id`` + ``version``) as one pending batch.

    Without a ``plan_id`` the sheet is planned first and committed at once.
    ESG-lite writes the rows; this service only marks the file imported and
    deletes it. A file is imported once: a second call answers 409.
    """
    document_id = _own_document(payload.get("document_id"), principal)
    payload = {**payload, "document_id": document_id}
    claimed, status = claim_uploaded_document_for_import(document_id)
    if not claimed:
        if status is None:
            raise HTTPException(status_code=422, detail="Invalid document_id")
        if status == "importing":
            raise HTTPException(status_code=409, detail="This file is already being imported.")
        raise HTTPException(status_code=409, detail="This file has already been imported. Please re-upload it to import again.")
    try:
        sheet, ctx = _sheet_rows(payload)
        with plans.PlanClient(_bearer(request)) as client:
            plan_id = payload.get("plan_id")
            version = payload.get("version")
            if not plan_id:
                if not sheet.rows:
                    raise ValueError("No rows to import. Check the category and unit columns and the ticked categories.")
                company_id = company_id_for_site(ctx["site_id"]) if principal.is_superadmin else None
                plan = plans.create_plan_from_rows(client, ctx["source_name"], sheet.rows, company_id)
                plan_id, version = plan["plan_id"], plan["version"]
            if version is None:
                raise ValueError("version is required with plan_id")
            res = plans.commit_plan(
                client,
                document_id=document_id,
                plan_id=str(plan_id),
                version=int(version),
                local_issues=sheet.local_issues,
                not_selected=sheet.not_selected,
                total_rows=sheet.total_rows,
            )
        # Rows are committed on ESG-lite, so the spreadsheet is no longer needed.
        delete_document_file(document_id, new_status="imported")
        return res
    except HTTPException:
        release_import_claim(document_id)
        raise
    except plans.EsgLiteError as e:
        release_import_claim(document_id)
        raise _esg_lite_http_error(e)
    except (TypeError, ValueError) as e:
        release_import_claim(document_id)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        release_import_claim(document_id)
        logger.error(f"import failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Import failed.")
