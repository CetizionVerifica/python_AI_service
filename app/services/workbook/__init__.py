"""XI1 · Workbook reader: a deterministic, model-free description of any
.xlsx/.xls/.csv (see CLAUDE.md in this folder)."""
from app.services.workbook.reader import (  # noqa: F401
    Column,
    DroppedRow,
    Sheet,
    Workbook,
    fingerprint_of,
    month_of_header,
    normalise_header,
    read_workbook,
    read_workbook_bytes,
)
