
from typing import List, Optional
from pydantic import BaseModel, Field, computed_field


class LineItem(BaseModel):
    description: str = Field(..., description="Description of the item or service")
    quantity: Optional[float] = Field(None, description="Quantity of items")
    unit: Optional[str] = Field(None, description="Unit of measurement for this line item (e.g., litre, kg, kWh)")
    unit_price: Optional[float] = Field(None, description="Price per unit")
    amount: Optional[float] = Field(None, description="Total amount for the line item")


class ActivityEntry(BaseModel):
    """A single consumed resource/commodity extracted from an invoice."""
    activity_description: Optional[str] = Field(
        None,
        description="What was consumed/purchased (e.g., Diesel, Electricity, LPG, Natural Gas, Petrol)"
    )
    total_quantity: Optional[float] = Field(
        None,
        description="Total physical quantity consumed for this activity"
    )
    unit_of_measurement: Optional[str] = Field(
        None,
        description="Physical unit of measurement (e.g., litre, kg, kWh, m³, gallon, tonne)"
    )
    emission_category: Optional[str] = Field(
        None,
        description="Matched emission category name from the known list provided in context"
    )
    column_values: Optional[dict[str, str]] = Field(
        None,
        description="Extracted dropdown field values keyed by column name (e.g., {'Waste Type': 'Process Organic Waste', 'Disposal Method': 'Incineration'})"
    )


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

    # Multiple activities per invoice
    activities: List[ActivityEntry] = Field(default_factory=list, description="Distinct resources/commodities consumed in this invoice")

    # Backward-compatible computed fields — return first activity's values
    @computed_field
    @property
    def activity_description(self) -> Optional[str]:
        return self.activities[0].activity_description if self.activities else None

    @computed_field
    @property
    def total_quantity(self) -> Optional[float]:
        return self.activities[0].total_quantity if self.activities else None

    @computed_field
    @property
    def unit_of_measurement(self) -> Optional[str]:
        return self.activities[0].unit_of_measurement if self.activities else None

    @computed_field
    @property
    def emission_category(self) -> Optional[str]:
        return self.activities[0].emission_category if self.activities else None


class CategorySuggestion(BaseModel):
    emission_category_name: str
    category_id: int
    category_name: str
    scope: Optional[str] = None
    denominator_unit: Optional[str] = None
    confidence: float = Field(..., description="Match confidence 0-100")


class EmissionReady(BaseModel):
    """Pre-built payload for Node.js POST /emissions — one per activity."""
    invoice_index: int = 0
    activity_index: int = 0
    site_id: Optional[int] = None
    category_id: Optional[int] = None
    activity_data: dict = {}
    activity_data_unit: Optional[str] = None
    date_of_reporting: Optional[str] = None
    total_emission: float = 0.0
    unit: str = "kg CO2e"
    vendor_name: Optional[str] = None


class ExtractionResponse(BaseModel):
    filename: str
    data: List[InvoiceData] = []
    error: Optional[str] = None
    validations: List[List[dict]] = []
    suggested_categories: List[Optional[CategorySuggestion]] = []
    emission: List[EmissionReady] = []
    cloudinary_url: Optional[str] = None
    invoice_id: Optional[int] = None
