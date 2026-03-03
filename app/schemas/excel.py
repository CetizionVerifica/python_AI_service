from pydantic import BaseModel, Field

class MappedDataRequest(BaseModel):
    document_id: int
    mappings: dict[str, str]  # { requiredField: excelColumnName }
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=500, ge=1, le=5000)