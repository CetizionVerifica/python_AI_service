from pydantic import BaseModel
from typing import Optional


class ParsedMappingRow(BaseModel):
    company_category_name: str
    global_category_name: str
    factor_value: Optional[float] = None
    unit: Optional[str] = None


class ParseMappingExcelResponse(BaseModel):
    filename: str
    mappings: list[ParsedMappingRow]
    warnings: list[str] = []
    total_records: int = 0
