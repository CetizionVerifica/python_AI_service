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


class BomLine(BaseModel):
    line: int = Field(..., description="Row number in the sheet, or the line's order in a PDF")
    values: dict[str, Any] = Field(..., description="Body for one line of ESG-lite PUT /pcf/studies/:id/inputs")
    confidence: int = Field(..., ge=0, le=100)
    reason: str
    warnings: list[str] = []
    problems: list[str] = Field([], description="ESG-lite would refuse the line as it is")
    by: Literal["sheet", "ai"]


class BomEvidence(BaseModel):
    id: int
    document_name: str
    url: str
    stored: Literal["cloud", "local"]


class BomResponse(BaseModel):
    file_name: str
    sheet_names: list[str]
    sheet_name: Optional[str]
    header_line: int
    headers: list[str]
    mapping: dict[str, ColumnMatch]
    batch_units: float
    lines: list[BomLine]
    total_lines: int
    lines_with_problems: int
    low_confidence_lines: int
    warnings: list[str]
    ai_used: bool
    evidence: Optional[BomEvidence] = Field(None, description="The raw file kept as evidence; null when it couldn't be stored")


class MatchLine(BaseModel):
    id: Optional[Any] = Field(None, description="Echoed back; the caller's line id")
    name: str
    unit: Optional[str] = None
    stage: Optional[str] = None
    material_group: Optional[str] = None
    origin_country: Optional[str] = None
    supplier_name: Optional[str] = None
    recycled_share_pct: Optional[float] = None


class FactorCandidate(BaseModel):
    material_factor_id: int
    name: str
    material_group: Optional[str] = None
    unit: Optional[str] = None
    geography: Optional[str] = None
    source: Optional[str] = None
    source_year: Optional[int] = None
    licence: Optional[str] = None
    recycled_variant: bool = False


class MatchRequest(BaseModel):
    lines: list[MatchLine] = Field(..., min_length=1, max_length=500)
    factors: list[FactorCandidate] = Field(..., min_length=1, max_length=5000, description="The MaterialFactor rows the caller may use (C04's list)")
    geography_hint: Optional[str] = Field(None, description="Site country or region, e.g. “Bahrain”")


class MatchSuggestion(BaseModel):
    material_factor_id: int
    name: Optional[str]
    confidence: int = Field(..., ge=0, le=100)
    reason: str
    by: Literal["rule", "ai"]


class MatchedLine(BaseModel):
    id: Optional[Any]
    name: str
    suggestions: list[MatchSuggestion]
    unmatched: bool = Field(..., description="No suggestion reaches 60; the person picks by hand")


class MatchResponse(BaseModel):
    lines: list[MatchedLine]
    total_lines: int
    unmatched_lines: int
    ai_used: bool
