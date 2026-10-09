"""Locks the pure calculation helpers the bulk import and OCR flows use."""
from app.schemas.invoice import InvoiceData
from app.services import excel_parser as ep
from app.services.pipeline import _resolve_emission_category_from_mapping
from app.services.validators import validate_totals

PER_METHOD = {
    "mode": "per_method",
    "method_column": "Calculation Method",
    "methods": {
        "Direct energy": {"multiply": ["Units Sold", "Energy per use", "Lifetime uses"]},
        "Share of energy": {"multiply": ["Units Sold", "Energy per use", "Share in use"], "percent": ["Share in use"]},
    },
}
PER_UNIT = {
    "mode": "per_unit",
    "legacy_field": "Distance travelled",
    "methods": {
        "tonne.km": {"multiply": ["Weight", "Distance travelled"]},
        "km": {"multiply": ["Distance travelled"]},
    },
}

SPEC_CASES = [
    ("per_method product", PER_METHOD, {"Calculation Method": "Direct energy", "Units Sold": "1,200", "Energy per use": "0.75", "Lifetime uses": "3650"}, "kwh"),
    ("per_method percent", PER_METHOD, {"Calculation Method": "Share of energy ", "Units Sold": "800", "Energy per use": "2.5", "Share in use": "40"}, "kwh"),
    ("per_method percent over 100", PER_METHOD, {"Calculation Method": "Share of energy", "Units Sold": "1", "Energy per use": "1", "Share in use": "140"}, "kwh"),
    ("per_method blank field", PER_METHOD, {"Calculation Method": "Direct energy", "Units Sold": "10", "Energy per use": "", "Lifetime uses": "5"}, "kwh"),
    ("per_method unknown method", PER_METHOD, {"Calculation Method": "Other", "Units Sold": "10"}, "kwh"),
    ("per_method nan method", PER_METHOD, {"Calculation Method": "nan"}, "kwh"),
    ("per_unit tonne.km variant", PER_UNIT, {"Weight": "22.5", "Distance travelled": "640"}, "Tonne KM"),
    ("per_unit tkm alias", PER_UNIT, {"Weight": "3", "Distance travelled": "100"}, "tkm"),
    ("per_unit km", PER_UNIT, {"Distance travelled": "1180"}, "kms"),
    ("per_unit legacy row", PER_UNIT, {"Distance travelled": "9000"}, "tonne.km"),
    ("per_unit unknown unit", PER_UNIT, {"Distance travelled": "5"}, "mile"),
    ("per_unit negative", PER_UNIT, {"Weight": "-1", "Distance travelled": "5"}, "tonne.km"),
]

ACTIVITY_CASES = [
    {"activity_value": "1000"},
    {"Quantity": "1,250.5"},
    {"meter_no": "7", "Units consumed": "25000"},
    {"Units consumed": "42"},
    {"fuel_type": "500", "litres": "12"},
    {"emission_category": "Diesel", "note": "n/a"},
    {"value": "0", "amount": "300"},
]

CONVERSIONS = [
    ("gallon", "litre"), ("litre", "gallon"), ("kl", "litre"), ("mwh", "kwh"), ("kwh", "gj"),
    ("gj", "kwh"), ("kg", "tonne"), ("tonne", "kg"), ("lb", "kg"), ("tonne.km", "kg.km"),
    ("Tonnes", "kg"), ("kg", "litre"),
]

UNITS_RAW = ["ton of material", "Tonnes", "per passenger.km", "KWH", "Litre", "(kg CO2 e/ km)", "litres", "Gallons"]
NUMERIC_RAW = [0.7499399999999999, "1,000", " 12.5 ", "-", "#N/A", True, 3, "abc", "1e3"]
DATES_RAW = ["03.04.2025", "2025-07-31", "", "not a date", "31/12/2025", "2025-02-30"]

# Same factor/value pairs as the ESG-lite API snapshot, so the two engines'
# rounding can be compared side by side.
ROUNDING = [
    (1000, 2680.0), (500 * 3.78541, 2680.0), (12500, 183.0), (25000, 708.0), (42, 708.0),
    (1.25, 708.0), (3285000, 0.233), (800, 0.233), (14400, 105.4), (1180, 250.12),
    (37.4, 467.01), (0.125, 100.0), (0.135, 100.0), (2.675, 1000.0),
]


def test_spec_activity_values(snapshot):
    out = []
    for name, spec, data, unit in SPEC_CASES:
        value, error = ep._compute_spec_activity_value(spec, data, unit)
        out.append({"case": name, "value": value, "error": error, "unit_key": ep._normalize_unit_key(unit)})
    snapshot("spec_activity_values", out)


def test_activity_value_heuristic(snapshot):
    snapshot("activity_value_heuristic", [{"row": row, "value": ep._extract_activity_value(row)} for row in ACTIVITY_CASES])


def test_unit_conversions(snapshot):
    snapshot("unit_conversions", [
        {"from": f, "to": t, "factor": ep.get_conversion_factor(f, t), "exact_match": ep.units_match_exact(f, t)}
        for f, t in CONVERSIONS
    ])


def test_parsing_helpers(snapshot):
    snapshot("parsing_helpers", {
        "units": {raw: ep._normalise_unit(raw) for raw in UNITS_RAW},
        "numbers": [[repr(raw), ep._parse_numeric(raw)] for raw in NUMERIC_RAW],
        "dates": {raw: ep._parse_row_date(raw, "2025-01-01") for raw in DATES_RAW},
        "factor_year": {d: ep._factor_year(d, 2000) for d in ["2025-03-31", "2026-01-01", "bad"]},
    })


def test_emission_rounding(snapshot):
    snapshot("emission_rounding", [
        {"activity": a, "factor": f, "total_tco2e": round((a * f) / 1000.0, 2)} for a, f in ROUNDING
    ])


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
