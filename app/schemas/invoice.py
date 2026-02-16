
from typing import List, Optional
from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str = Field(..., description="Description of the item or service")
    quantity: Optional[float] = Field(None, description="Quantity of items")
    unit: Optional[str] = Field(None, description="Unit of measurement for this line item (e.g., litre, kg, kWh)")
    unit_price: Optional[float] = Field(None, description="Price per unit")
    amount: Optional[float] = Field(None, description="Total amount for the line item")


class InvoiceData(BaseModel):
    invoice_number: Optional[str] = Field(None, description="Unique identifier for the invoice")
    invoice_date: Optional[str] = Field(None, description="Date of the invoice (YYYY-MM-DD)")
    vendor_name: Optional[str] = Field(None, description="Name of the vendor or supplier")
    vendor_address: Optional[str] = Field(None, description="Address of the vendor")
    subtotal: Optional[float] = Field(None, description="Subtotal amount before tax")
    total_amount: Optional[float] = Field(None, description="Total amount due")
    currency: Optional[str] = Field("USD", description="Currency code (e.g., USD, EUR, INR)")
    tax_amount: Optional[float] = Field(None, description="Total tax amount")
    line_items: List[LineItem] = Field(default_factory=list, description="List of items in the invoice")

    # Emission-relevant fields
    activity_description: Optional[str] = Field(
        None,
        description="What was consumed/purchased (e.g., Diesel, Electricity, LPG, Natural Gas, Petrol)"
    )
    total_quantity: Optional[float] = Field(
        None,
        description="Total physical quantity consumed (sum of line item quantities)"
    )
    unit_of_measurement: Optional[str] = Field(
        None,
        description="Physical unit of measurement (e.g., litre, kg, kWh, m³, gallon, tonne)"
    )


class CategorySuggestion(BaseModel):
    emission_category_name: str
    category_id: int
    category_name: str
    scope: Optional[str] = None
    denominator_unit: Optional[str] = None
    confidence: float = Field(..., description="Match confidence 0-100")


class EmissionReady(BaseModel):
    """Pre-built payload for Node.js POST /emissions — one per invoice."""
    site_id: Optional[int] = None
    category_id: Optional[int] = None
    activity_data: dict = {}
    activity_data_unit: Optional[str] = None
    date_of_reporting: Optional[str] = None
    total_emission: float = 0.0
    unit: str = "kg CO2e"


class ExtractionResponse(BaseModel):
    filename: str
    data: List[InvoiceData] = []
    error: Optional[str] = None
    validations: List[List[dict]] = []
    suggested_categories: List[Optional[CategorySuggestion]] = []
    emission: List[EmissionReady] = []
