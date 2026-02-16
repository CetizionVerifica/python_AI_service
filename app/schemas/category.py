from pydantic import BaseModel, Field
from typing import Optional

class EmissionCategory(BaseModel):
    emission_category_name: str = Field(..., description="Unique name of the emission source (e.g., 'Diesel')")
    denominator_unit: Optional[str] = Field(None, description="Standard unit for this emission factor")
    category_id: int
    category_name: str
    scope: Optional[str] = None
