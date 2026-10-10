"""Parsed emission factors carry the 4 decimal places the database keeps
(emission_factors.factor_value is numeric(10,4)), and any factor that loses
digits is flagged rather than silently truncated (F-19)."""
import openpyxl


def test_factors_are_rounded_to_stored_precision_and_flagged():
    from app.schemas.emission_factor import DescriptorColumn, SpreadsheetSchema, YearMapping
    from app.services import excel_parser as ep

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fuel", "Unit", "2024"])
    ws.append(["Diesel", "kgCO2e/litre", 2.68])
    ws.append(["Trace gas", "kgCO2e/kg", 0.000123])
    ws.append(["Natural gas", "kgCO2e/kwh", 0.18316])
    schema = SpreadsheetSchema(
        layout_type="simple",
        descriptor_columns=[DescriptorColumn(column_index=1, header_name="Fuel")],
        unit_column=DescriptorColumn(column_index=2, header_name="Unit"),
        data_start_row=2,
        years=[YearMapping(year=2024, value_column=3)],
    )

    factors, warnings = ep._extract_factors(ws, schema)

    assert [(f.emission_category_name, f.factor_value) for f in factors] == [
        ("Diesel", 2.68),
        ("Trace gas", 0.0001),
        ("Natural gas", 0.1832),
    ]
    assert warnings == [
        "Row 3: factor 0.000123 for 'Trace gas' is stored as 0.0001 (the database keeps 4 decimal places)",
        "Row 4: factor 0.18316 for 'Natural gas' is stored as 0.1832 (the database keeps 4 decimal places)",
    ]
