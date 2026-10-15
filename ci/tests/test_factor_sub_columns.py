"""S3-2: a factor sheet with Direct, WTT and Total plus gas columns gives one
candidate per sub-column, each with its factor type; gas columns become parts
of the direct factor (layer rule 2, app/scope3/CLAUDE.md)."""
import openpyxl

from app.schemas.emission_factor import DescriptorColumn, SpreadsheetSchema, SubColumn, YearMapping


def _sheet(title="Fuels"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    ws.append(["Fuel", "Unit", "Direct", "WTT", "Total", "kg CO2", "kg CH4", "kg N2O", "Outside of scopes"])
    ws.append(["Diesel", "kg CO2e/litre", 2.51279, 0.61101, 3.1238, 2.47703, 0.00019, 0.03557, 0.17405])
    ws.append(["Petrol", "kg CO2e/litre", 2.0844, None, 2.0844, None, None, None, None])
    return ws


def _schema():
    subs = ["Direct", "WTT", "Total", "kg CO2", "kg CH4", "kg N2O", "Outside of scopes"]
    return SpreadsheetSchema(
        layout_type="sub_columns",
        descriptor_columns=[DescriptorColumn(column_index=1, header_name="Fuel")],
        unit_column=DescriptorColumn(column_index=2, header_name="Unit"),
        data_start_row=2,
        years=[
            YearMapping(
                year=2026,
                sub_columns=[SubColumn(name=n, column_index=i + 3) for i, n in enumerate(subs)],
                primary_sub_column="Total",
            )
        ],
    )


def test_one_candidate_per_sub_column_with_type_and_gas_parts():
    from app.services import excel_parser as ep

    factors, warnings = ep._extract_factors(_sheet(), _schema())
    got = [
        (f.emission_category_name, f.sub_column, f.factor_type, f.factor_value_text, f.primary, f.includes_wtt)
        for f in factors
    ]
    assert got == [
        ("Diesel - Direct", "Direct", "combustion", "2.51279", False, False),
        ("Diesel - WTT", "WTT", "wtt", "0.61101", False, False),
        ("Diesel", "Total", "life_cycle", "3.1238", True, True),
        # Petrol has no WTT figure: its Total is still flagged, because the
        # sheet's Total sums in its WTT column wherever one is given.
        ("Petrol - Direct", "Direct", "combustion", "2.0844", False, False),
        ("Petrol", "Total", "life_cycle", "2.0844", True, True),
    ]
    direct = factors[0]
    assert (direct.co2_kg, direct.ch4_kg, direct.n2o_kg, direct.biogenic_co2_kg) == (
        "2.47703", "0.00019", "0.03557", "0.17405",
    )
    # Gas parts go on the direct factor only.
    assert all(f.co2_kg is None for f in factors[1:])
    assert all(f.denominator_unit == "litre" for f in factors)
    assert warnings == []


def test_gas_parts_go_on_the_primary_when_there_is_no_direct_column():
    from app.services import excel_parser as ep

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fuel", "Unit", "kg CO2e", "kg CO2", "kg CH4"])
    ws.append(["Coal", "kg CO2e/tonne", 2403.84, 2380.0, 1.2])
    schema = SpreadsheetSchema(
        layout_type="sub_columns",
        descriptor_columns=[DescriptorColumn(column_index=1, header_name="Fuel")],
        unit_column=DescriptorColumn(column_index=2, header_name="Unit"),
        data_start_row=2,
        years=[YearMapping(year=2026, sub_columns=[
            SubColumn(name="kg CO2e", column_index=3),
            SubColumn(name="kg CO2", column_index=4),
            SubColumn(name="kg CH4", column_index=5),
        ], primary_sub_column="kg CO2e")],
    )
    factors, _ = ep._extract_factors(ws, schema)
    assert len(factors) == 1
    f = factors[0]
    assert (f.emission_category_name, f.factor_type, f.co2_kg, f.ch4_kg) == ("Coal", "combustion", "2380", "1.2")


def test_sheet_label_sets_the_type():
    from app.services import excel_parser as ep

    # A life-cycle sheet: Direct and Total are life_cycle factors.
    factors, _ = ep._extract_factors(_sheet("Freight life cycle"), _schema())
    assert [f.factor_type for f in factors if f.sub_column == "Direct"] == ["life_cycle", "life_cycle"]

    # A one-value WTT sheet: every factor is wtt.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "WTT- fuels"
    ws.append(["Fuel", "Unit", "2026"])
    ws.append(["Diesel", "kg CO2e/litre", 0.61101])
    schema = SpreadsheetSchema(
        layout_type="simple",
        descriptor_columns=[DescriptorColumn(column_index=1, header_name="Fuel")],
        unit_column=DescriptorColumn(column_index=2, header_name="Unit"),
        data_start_row=2,
        years=[YearMapping(year=2026, value_column=3)],
    )
    factors, _ = ep._extract_factors(ws, schema)
    assert [(f.factor_type, f.factor_value_text) for f in factors] == [("wtt", "0.61101")]

    ws.title = "Fuels"
    factors, _ = ep._extract_factors(ws, schema)
    assert factors[0].factor_type is None


def test_gas_label_reader():
    from app.services.excel_parser import _gas_part

    assert _gas_part("kg CO2") == "co2_kg"
    assert _gas_part("CO₂") == "co2_kg"
    assert _gas_part("kg CO2e") is None
    assert _gas_part("kg CO2 e") is None
    assert _gas_part("CO2 equivalent") is None
    assert _gas_part("kg CH4") == "ch4_kg"
    # DEFRA's per-gas shares are in CO2e: CO2's is kg of CO2, the others are not kg of the gas.
    assert _gas_part("kg CO2e of CO2 per unit") == "co2_kg"
    assert _gas_part("kg CO2e of CH4 per unit") == "gas_share_in_co2e"
    assert _gas_part("kg CO2e of N2O per unit") == "gas_share_in_co2e"
    assert _gas_part("kg N2O") == "n2o_kg"
    assert _gas_part("Outside of scopes") == "biogenic_co2_kg"
    assert _gas_part("Total") is None
    assert _gas_part("WTT") is None


def test_gas_shares_in_co2e_are_left_out_with_a_warning():
    from app.services import excel_parser as ep

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fuel", "Unit", "kg CO2e", "kg CO2e of CO2 per unit", "kg CO2e of CH4 per unit", "kg CO2e of N2O per unit"])
    ws.append(["Diesel", "kg CO2e/litre", 2.51279, 2.47703, 0.00019, 0.03557])
    ws.append(["Petrol", "kg CO2e/litre", 2.0844, 2.0, 0.01, 0.0744])
    names = ["kg CO2e", "kg CO2e of CO2 per unit", "kg CO2e of CH4 per unit", "kg CO2e of N2O per unit"]
    schema = SpreadsheetSchema(
        layout_type="sub_columns",
        descriptor_columns=[DescriptorColumn(column_index=1, header_name="Fuel")],
        unit_column=DescriptorColumn(column_index=2, header_name="Unit"),
        data_start_row=2,
        years=[YearMapping(year=2026, sub_columns=[SubColumn(name=n, column_index=i + 3) for i, n in enumerate(names)], primary_sub_column="kg CO2e")],
    )
    factors, warnings = ep._extract_factors(ws, schema)
    assert [(f.emission_category_name, f.co2_kg, f.ch4_kg, f.n2o_kg) for f in factors] == [
        ("Diesel", "2.47703", None, None),
        ("Petrol", "2", None, None),
    ]
    assert len(warnings) == 2
    assert all("gas share in CO2e" in w for w in warnings)
