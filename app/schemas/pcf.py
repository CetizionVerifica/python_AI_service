"""Responses of the PCF AI assist endpoints (E2). Confidence is 0–100; below
60 the UI shows the warn tint and the person must confirm the line."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ColumnMatch(BaseModel):
    header: str
    confidence: int = Field(..., ge=0, le=100)
    reason: str
    by: Literal["header", "ai", "person"]


class SheetUnit(BaseModel):
    unit: str
    reason: str


class FactorRow(BaseModel):
    line: int = Field(..., description="Row number as people see it in Excel")
    values: dict[str, Any] = Field(..., description="Body for ESG-lite POST /pcf/material-factors/import")
    confidence: int = Field(..., ge=0, le=100)
    reason: str
    warnings: list[str] = []
    problems: list[str] = Field([], description="ESG-lite would refuse the row as it is")


class FactorSheetResponse(BaseModel):
    file_name: str
    sheet_names: list[str]
    sheet_name: Optional[str]
    header_line: int
    headers: list[str]
    mapping: dict[str, ColumnMatch]
    sheet_unit: Optional[SheetUnit] = None
    rows: list[FactorRow]
    total_rows: int
    rows_with_problems: int
    low_confidence_rows: int
    warnings: list[str]
    ai_used: bool
