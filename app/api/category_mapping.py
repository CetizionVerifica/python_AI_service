import logging
import re

from fastapi import APIRouter, UploadFile, File, HTTPException
import openpyxl

from app.services import storage
from app.schemas.category_mapping import (
    ParsedMappingRow,
    ParseMappingExcelResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Header patterns to auto-detect columns
COMPANY_HEADER_PATTERNS = [
    r"company.*(?:raw|material|cat|category|internal|name)",
    r"raw.*material",
    r"internal.*category",
    r"company.*name",
]
GLOBAL_HEADER_PATTERNS = [
    r"ef.*category",
    r"global.*category",
    r"external.*category",
    r"emission.*factor.*category",
    r"standard.*category",
]
FACTOR_HEADER_PATTERNS = [
    r"emission.*factor",
    r"factor.*value",
    r"ef.*value",
]
UNIT_HEADER_PATTERNS = [
    r"unit",
]


def _match_header(header: str, patterns: list[str]) -> bool:
    """Check if a header matches any pattern (case-insensitive)."""
    header_lower = header.lower().strip()
    return any(re.search(p, header_lower) for p in patterns)


def _detect_columns(headers: list[str]) -> dict[str, int | None]:
    """
    Auto-detect which column index maps to which field.
    Returns dict with keys: company, global, factor, unit.
    """
    result: dict[str, int | None] = {
        "company": None,
        "global": None,
        "factor": None,
        "unit": None,
    }

    for i, header in enumerate(headers):
        if not header:
            continue
        h = str(header)
        if result["company"] is None and _match_header(h, COMPANY_HEADER_PATTERNS):
            result["company"] = i
        elif result["global"] is None and _match_header(h, GLOBAL_HEADER_PATTERNS):
            result["global"] = i
        elif result["factor"] is None and _match_header(h, FACTOR_HEADER_PATTERNS):
            result["factor"] = i
        elif result["unit"] is None and _match_header(h, UNIT_HEADER_PATTERNS):
            result["unit"] = i

    # Fallback: if headers weren't detected, assume Column A = company, Column B = global
    if result["company"] is None and result["global"] is None and len(headers) >= 2:
        result["company"] = 0
        result["global"] = 1
        if len(headers) >= 3:
            result["factor"] = 2
        if len(headers) >= 4:
            result["unit"] = 3

    return result


def _parse_numeric(value) -> float | None:
    """Safely convert a cell value to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s in ("-", "N/A", "n/a", "NA", ""):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


@router.post(
    "/category-mappings/parse-excel",
    response_model=ParseMappingExcelResponse,
)
async def parse_category_mapping_excel(
    file: UploadFile = File(...),
):
    """
    Parse a category mapping Excel file.
    Expects a flat table with columns for company category name and global category name.
    Returns structured data for preview before bulk creation.
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

        wb = openpyxl.load_workbook(str(file_path), data_only=True)
        ws = wb.active

        if ws is None:
            raise HTTPException(status_code=422, detail="No active sheet found")

        # Read headers from row 1
        header_row = []
        for cell in ws[1]:
            header_row.append(str(cell.value) if cell.value is not None else "")

        col_map = _detect_columns(header_row)
        warnings: list[str] = []

        if col_map["company"] is None or col_map["global"] is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Could not detect company category and global category columns. "
                    "Expected headers like 'Company raw material cat.' and 'EF category'."
                ),
            )

        # Extract data rows
        mappings: list[ParsedMappingRow] = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not row or all(v is None for v in row):
                continue

            company_val = row[col_map["company"]] if col_map["company"] < len(row) else None
            global_val = row[col_map["global"]] if col_map["global"] < len(row) else None

            if not company_val or not global_val:
                warnings.append(f"Row {row_idx}: missing company or global category name, skipped")
                continue

            company_name = str(company_val).strip()
            global_name = str(global_val).strip()

            if not company_name or not global_name:
                warnings.append(f"Row {row_idx}: empty company or global category name, skipped")
                continue

            factor_value = None
            if col_map["factor"] is not None and col_map["factor"] < len(row):
                factor_value = _parse_numeric(row[col_map["factor"]])

            unit = None
            if col_map["unit"] is not None and col_map["unit"] < len(row):
                unit_val = row[col_map["unit"]]
                if unit_val:
                    unit = str(unit_val).strip()

            mappings.append(
                ParsedMappingRow(
                    company_category_name=company_name,
                    global_category_name=global_name,
                    factor_value=factor_value,
                    unit=unit,
                )
            )

        wb.close()

        if not mappings:
            raise HTTPException(
                status_code=422,
                detail="No mapping data could be extracted from this file.",
            )

        return ParseMappingExcelResponse(
            filename=filename,
            mappings=mappings,
            warnings=warnings,
            total_records=len(mappings),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Mapping Excel parsing failed for {filename}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to parse file: {str(e)}"
        )
    finally:
        if file_path:
            storage.cleanup(file_path)
