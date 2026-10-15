from pydantic import BaseModel
from typing import Optional


class DescriptorColumn(BaseModel):
    column_index: int
    header_name: str


class SubColumn(BaseModel):
    name: str
    column_index: int


class YearMapping(BaseModel):
    year: int
    value_column: Optional[int] = None
    sub_columns: Optional[list[SubColumn]] = None
    primary_sub_column: Optional[str] = None
    disposal_columns: Optional[list[SubColumn]] = None


class SpreadsheetSchema(BaseModel):
    layout_type: str  # "simple" | "sub_columns" | "disposal_pivot"
    descriptor_columns: list[DescriptorColumn]
    parent_category_column: Optional[DescriptorColumn] = None
    include_parent_in_name: bool = False
    source_column: Optional[DescriptorColumn] = None
    unit_column: Optional[DescriptorColumn] = None
    data_start_row: int
    years: list[YearMapping]
    descriptor_join_separator: str = " - "
    notes: Optional[str] = None


class EmissionFactorRecord(BaseModel):
    year: int
    # A float for older readers; ``factor_value_text`` is the exact figure
    # (full precision, plain decimal) that ESG-lite should store (S3-2).
    factor_value: float
    factor_value_text: Optional[str] = None
    denominator_unit: Optional[str] = None
    source: Optional[str] = None
    emission_category_name: Optional[str] = None
    parent_category: Optional[str] = None
    # S3-2 factor metadata, read from the sheet's own labels. ESG-lite
    # validates them; a person confirms them in the import preview.
    factor_type: Optional[str] = None  # FactorType of ESG-lite src/scope3/contracts/factor.ts
    sub_column: Optional[str] = None  # the sub-column this candidate came from ("Total", "WTT")
    primary: bool = True  # the sub-column the sheet's schema names as the factor (older imports took only this one)
    includes_wtt: bool = False  # a total that sums in a WTT sub-column of the same sheet
    co2_kg: Optional[str] = None
    ch4_kg: Optional[str] = None
    n2o_kg: Optional[str] = None
    biogenic_co2_kg: Optional[str] = None


# ---------------------------------------------------------------------------
# Category inference schemas
# ---------------------------------------------------------------------------

class DbCategory(BaseModel):
    id: int
    name: str


class CategorySuggestion(BaseModel):
    parent_category: str
    suggested_category_id: Optional[int] = None
    suggested_category_name: Optional[str] = None
    confidence: str = "low"  # "high" | "medium" | "low"


class ColumnHeader(BaseModel):
    """A column header from the spreadsheet, for user-facing column mapping UI."""
    column_index: int
    header_name: str
    sample_values: list[str] = []  # first few non-empty values for preview


class ParseExcelResponse(BaseModel):
    filename: str
    factors: list[EmissionFactorRecord]
    schema_detected: SpreadsheetSchema
    warnings: list[str] = []
    total_records: int = 0
    available_years: list[int] = []
    parent_categories: list[str] = []
    category_suggestions: list[CategorySuggestion] = []
    upload_id: Optional[int] = None
    cloudinary_url: Optional[str] = None
    # Sheet & column metadata for user override UI
    sheet_names: list[str] = []
    selected_sheet: Optional[str] = None
    available_columns: list[ColumnHeader] = []


class EmissionFactorUploadRecord(BaseModel):
    id: int
    file_name: str
    cloudinary_url: str
    cloudinary_public_id: str
    file_size: Optional[int] = None
    uploaded_by: Optional[int] = None
    site_id: Optional[int] = None
    category_ids: Optional[list[int]] = None
    layout_type: Optional[str] = None
    total_records: int = 0
    records_created: int = 0
    records_skipped: int = 0
    status: str = "parsed"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class UpdateUploadResultsRequest(BaseModel):
    records_created: int
    records_skipped: int
    status: str = "completed"
    site_id: Optional[int] = None
    category_ids: Optional[list[int]] = None
