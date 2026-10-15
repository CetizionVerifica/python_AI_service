"""Parsed emission factors keep the sheet's own precision (S3-2: ESG-lite
stores factor_value as numeric(18,8)). Each factor also travels as an exact
decimal string, ``factor_value_text``."""
import openpyxl


def _schema(**kw):
    from app.schemas.emission_factor import DescriptorColumn, SpreadsheetSchema, YearMapping

    return SpreadsheetSchema(
        layout_type="simple",
        descriptor_columns=[DescriptorColumn(column_index=1, header_name="Fuel")],
        unit_column=DescriptorColumn(column_index=2, header_name="Unit"),
        data_start_row=2,
        years=[YearMapping(year=2026, value_column=3)],
        **kw,
    )


def test_factors_keep_full_precision_and_are_not_flagged():
    from app.services import excel_parser as ep

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fuel", "Unit", "2026"])
    ws.append(["Diesel", "kgCO2e/litre", 2.68])
    ws.append(["Trace gas", "kgCO2e/kg", 0.00012345])
    ws.append(["Natural gas", "kgCO2e/kwh", 0.18316])
    ws.append(["Formula noise", "kgCO2e/kwh", 0.7499399999999999])
    ws.append(["Text cell", "kgCO2e/kwh", " 0.000000015 "])
    ws.append(["Tiny float", "kgCO2e/kwh", 1.5e-08])

    factors, warnings = ep._extract_factors(ws, _schema())

    assert [(f.emission_category_name, f.factor_value_text) for f in factors] == [
        ("Diesel", "2.68"),
        ("Trace gas", "0.00012345"),
        ("Natural gas", "0.18316"),
        ("Formula noise", "0.74994"),
        ("Text cell", "0.000000015"),
        ("Tiny float", "0.000000015"),
    ]
    assert factors[1].factor_value == 0.00012345
    assert warnings == []


def test_non_numbers_are_skipped():
    from app.services import excel_parser as ep

    for cell in (None, "", "-", "N/A", "#REF!", "abc", True, "nan", "inf"):
        assert ep._factor_text(cell) is None, cell
    assert ep._factor_text(100) == "100"
    assert ep._factor_text("1E-5") == "0.00001"
    assert ep._factor_text(-0.0) == "0"
