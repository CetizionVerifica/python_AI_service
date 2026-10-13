"""Locks the pure parsing helpers the sheet reader and the OCR flow use.

The bulk import's calculation left this service with IM11 (ESG-lite's plans
API does it; see app/services/excel_plan_client.py), so its spec products,
value heuristic, unit conversions and rounding are locked by ESG-lite's own
golden snapshot now, not here.
"""
from app.schemas.invoice import InvoiceData
from app.services import excel_parser as ep
from app.services.pipeline import _resolve_emission_category_from_mapping
from app.services.validators import validate_totals


UNITS_RAW = ["ton of material", "Tonnes", "per passenger.km", "KWH", "Litre", "(kg CO2 e/ km)", "litres", "Gallons"]
NUMERIC_RAW = [0.7499399999999999, "1,000", " 12.5 ", "-", "#N/A", True, 3, "abc", "1e3"]
DATES_RAW = ["03.04.2025", "2025-07-31", "", "not a date", "31/12/2025", "2025-02-30"]


def test_parsing_helpers(snapshot):
    snapshot("parsing_helpers", {
        "units": {raw: ep._normalise_unit(raw) for raw in UNITS_RAW},
        "numbers": [[repr(raw), ep._parse_numeric(raw)] for raw in NUMERIC_RAW],
        "dates": {raw: ep._parse_row_date(raw, "2025-01-01") for raw in DATES_RAW},
    })


def test_invoice_total_validation(snapshot):
    cases = [
        {"subtotal": "1,000.00", "tax_amount": "180.00", "total_amount": "1,180.00"},
        {"subtotal": "1000", "tax_amount": "180", "total_amount": "1180.05"},
        {"subtotal": "₹ 2,50,000.50", "tax_amount": "45000.09", "total_amount": "295000.59"},
        {"subtotal": None, "tax_amount": "1", "total_amount": "1"},
    ]
    snapshot("invoice_total_validation", [
        {"input": c, "checks": validate_totals(InvoiceData.model_construct(**c))} for c in cases
    ])


def test_category_mapping_resolution(snapshot):
    config = {
        "emission_category_mapping": {
            "Process Organic Waste|Incineration": "Process Organic Waste - Incineration",
            "Diesel": "Diesel (average biofuel blend)",
        },
        "column_dependencies": {"Disposal": "Waste Type"},
    }
    flat = {"emission_category_mapping": config["emission_category_mapping"]}
    snapshot("category_mapping_resolution", [
        _resolve_emission_category_from_mapping({"Waste Type": "Process Organic Waste", "Disposal": "Incineration"}, config),
        _resolve_emission_category_from_mapping({"Waste Type": "process organic waste", "Disposal": "INCINERATION"}, config),
        _resolve_emission_category_from_mapping({"Waste Type": "Metal", "Disposal": "Landfill"}, config),
        _resolve_emission_category_from_mapping({"Fuel": "Diesel"}, flat),
        _resolve_emission_category_from_mapping(None, config),
    ])
