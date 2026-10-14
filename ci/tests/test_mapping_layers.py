"""XI2 layers 1–2 on the XI1 fixtures with a fake context: the profile
layer re-validates and makes no model call, the rules layer maps the messy
layouts, names outside the context are dropped, and a second company's
names never appear. No database, no network, no model."""
import copy

import pytest

from app.schemas.mapping import MappingProposal
from app.services.mapping.engine import propose
from app.services.mapping.synonyms import part_of_header
from app.services.workbook import read_workbook
from ci.tests.conftest import FIXTURES
from ci.tests.mapping_fixtures import ACTIVITY_NAMES, OTHER_ACTIVITY_NAMES, ctx, lookup

XI1 = FIXTURES / "xi1"


def proposal_for(name, profiles=None, site_id=2):
    wb = read_workbook(XI1 / name)
    return propose(wb, 1, ctx(site_id), profiles or [], lookup())


def fields(sheet):
    return {f.key: f.column for f in sheet.fields}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_single_sheet_monthly_maps_every_part_from_the_headers():
    s = proposal_for("01-single-sheet-monthly.xlsx").sheets[0]
    assert fields(s) == {"activity_name": "Activity", "activity_value": "Quantity", "activity_unit": "Unit", "supplier": None}
    assert s.period.kind == "column" and s.period.column == "Month"
    # "HSD" is a Stationary combustion name only, so the sheet's category follows its activities.
    assert s.category.kind == "fixed" and s.category.id == 3 and s.category.confidence == 0.85
    assert [(a.sheet_text, a.factor_name, a.source) for a in s.activities] == [("HSD", "HSD", "rules")]
    assert s.source == "rules" and s.confidence >= 0.8


def test_sheet_named_after_a_category_is_that_category():
    p = proposal_for("04-one-sheet-per-category.xlsx")
    assert [(s.sheet_name, s.category.id, s.category.confidence) for s in p.sheets] == [("Stationary combustion", 3, 1.0), ("Purchased electricity", 5, 1.0), ("Business travel", 6, 1.0)]
    travel = p.sheets[2]
    # The distance form's field claims "Distance (km)"; the generic value stays empty.
    assert fields(travel)["distance"] == "Distance (km)" and fields(travel)["activity_value"] is None
    assert fields(travel)["passengers"] == "Passengers"


def test_category_column_and_site_column_in_one_sheet():
    s = proposal_for("03-multi-site-one-sheet.xlsx").sheets[0]
    assert s.category.kind == "column" and s.category.column == "Category"
    assert [(v.text, v.id) for v in s.category.values] == [("Stationary combustion", 3)]
    assert s.site_column.column == "Plant"
    assert [(v.text, v.matches_site) for v in s.site_column.values] == [("Plant 2", True), ("Plant 9", False)]
    assert s.not_in_context == ["Plant 9"]
    assert s.site_id == 2


def test_yearly_sheets_are_read_as_cy_and_fy():
    cy = proposal_for("05-yearly-cy.xlsx").sheets[0]
    assert cy.period.model_dump() == {"kind": "yearly", "year_type": "CY", "year": 2024}
    assert cy.category.kind == "column" and [v.id for v in cy.category.values] == [3, 4, 5]
    fy = proposal_for("06-yearly-fy.xlsx").sheets[0]
    assert fy.period.model_dump() == {"kind": "yearly", "year_type": "FY", "year": 2024}
    # "Scope" is not a category column: none of its values is a category name.
    assert fy.category.kind == "fixed"
    assert fy.confidence < 0.8


def test_months_as_columns_period_with_month_end_dates():
    s = proposal_for("07-months-as-columns.xlsx").sheets[0]
    assert s.layout == "months_as_columns" and s.period.kind == "months_as_columns"
    assert [c.date for c in s.period.columns][:3] == ["2025-01-31", "2025-02-28", "2025-03-31"]
    assert fields(s)["activity_value"] is None and fields(s)["activity_unit"] == "Unit"
    # Diesel belongs to two categories here, so the category stays for the user to confirm.
    assert s.confidence < 0.8


def test_electricity_bills_prefer_the_physical_quantity_over_the_amount():
    s = proposal_for("02-electricity-bills-date-column.xlsx").sheets[0]
    f = fields(s)
    assert f["activity_value"] == "Units consumed (kWh)" and f["supplier"] == "Supplier"
    assert s.period.column == "Bill date"


def test_mixed_units_value_column_is_claimed_with_low_confidence():
    s = proposal_for("08-mixed-units.xlsx").sheets[0]
    value = next(x for x in s.fields if x.key == "activity_value")
    assert value.column == "Consumption" and value.confidence < 0.8
    assert {a.sheet_text: a.factor_name for a in s.activities} == {"Diesel": "Diesel", "LPG": "LPG", "Coal": None}


def test_large_ledger_resolves_everything_above_threshold():
    s = proposal_for("12-large-ledger.csv").sheets[0]
    assert s.confidence >= 0.8 and s.category.kind == "column" and s.category.values[0].id == 4
    assert {a.sheet_text for a in s.activities} == {"CNG", "Diesel", "Petrol"}
    assert all(a.factor_name == a.sheet_text for a in s.activities)


def test_hostile_cells_change_nothing_but_the_text_they_hold():
    s = proposal_for("11-hostile-cell.xlsx").sheets[0]
    assert s.site_id == 2 and fields(s)["activity_value"] == "Qty"
    assert all(a.sheet_text == "Diesel" for a in s.activities)
    assert "Plant 9" not in s.not_in_context


def test_proposal_validates_against_the_contract_and_names_only_context_ids():
    for name in ("03-multi-site-one-sheet.xlsx", "05-yearly-cy.xlsx", "07-months-as-columns.xlsx"):
        p = proposal_for(name)
        MappingProposal.model_validate(p.model_dump())
        for s in p.sheets:
            assert s.site_id == 2
            ids = [s.category.id] if s.category.kind == "fixed" else [v.id for v in s.category.values if v.id]
            assert set(ids) <= {3, 4, 5, 6}
            for a in s.activities:
                assert a.category_id in (3, 4, 5, 6)
                assert a.factor_name is None or a.factor_name in ACTIVITY_NAMES[a.category_id]


def test_another_companys_names_never_appear():
    for name in ("01-single-sheet-monthly.xlsx", "03-multi-site-one-sheet.xlsx", "12-large-ledger.csv"):
        text = proposal_for(name).model_dump_json()
        for secret in ["Zed", "Process emissions", *OTHER_ACTIVITY_NAMES[30]]:
            assert secret not in text


def test_a_site_outside_the_context_is_refused():
    with pytest.raises(ValueError):
        proposal_for("01-single-sheet-monthly.xlsx", site_id=7)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def saved_profile_for(name):
    """A profile confirmed on the fixture's own rules proposal, as XI3 would store it."""
    sheet = proposal_for(name).sheets[0]
    return {"id": "0f6a6e3c-1111-4aaa-8bbb-ccccccccdddd", "company_id": 1, "site_id": 2, "category_id": None, "fingerprint": sheet.fingerprint, "sheet_proposal": sheet.model_dump(), "uses": 1, "created_at": "2026-10-14T00:00:00Z", "updated_at": "2026-10-14T00:00:00Z"}


def test_profile_hit_maps_with_confidence_one_and_no_model_call():
    profile = saved_profile_for("07-months-as-columns.xlsx")
    profile["sheet_proposal"]["category"] = {"kind": "fixed", "id": 4, "label": "Mobile combustion", "confidence": 1.0, "source": "profile"}
    p = proposal_for("07-months-as-columns.xlsx", profiles=[profile])
    s = p.sheets[0]
    assert p.model.cached_profile_hits == 1 and p.model.calls == 0
    assert s.category.kind == "fixed" and s.category.id == 4 and s.category.source == "profile"
    assert s.period.kind == "months_as_columns" and s.confidence >= 0.8
    assert {f.key: f.source for f in s.fields}["activity_unit"] == "profile"


def test_profile_for_another_site_or_fingerprint_is_ignored():
    profile = saved_profile_for("01-single-sheet-monthly.xlsx")
    other_site = {**profile, "site_id": 9}
    other_fp = {**profile, "fingerprint": "0" * 64}
    p = proposal_for("01-single-sheet-monthly.xlsx", profiles=[other_site, other_fp])
    assert p.model.cached_profile_hits == 0 and p.sheets[0].source == "rules"


def test_profile_parts_that_vanished_from_the_context_fall_to_the_rules():
    profile = saved_profile_for("01-single-sheet-monthly.xlsx")
    profile["sheet_proposal"]["category"] = {"kind": "fixed", "id": 99, "label": "Gone", "confidence": 1.0, "source": "profile"}
    profile["sheet_proposal"]["activities"] = [{"sheet_text": "HSD", "category_id": 3, "factor_name": "Vanished factor", "confidence": 1.0, "source": "profile", "save_mapping": False}]
    s = proposal_for("01-single-sheet-monthly.xlsx", profiles=[profile]).sheets[0]
    assert s.category.id == 3 and s.category.source == "rules"
    assert s.activities[0].factor_name == "HSD" and s.activities[0].source == "rules"
    assert s.source == "mixed"


def test_profile_fixed_period_is_not_reused_for_a_new_file():
    profile = saved_profile_for("05-yearly-cy.xlsx")
    profile["sheet_proposal"]["period"] = {"kind": "yearly", "year_type": "CY", "year": 2019}
    s = proposal_for("05-yearly-cy.xlsx", profiles=[profile]).sheets[0]
    assert s.period.model_dump() == {"kind": "yearly", "year_type": "CY", "year": 2024}


# ---------------------------------------------------------------------------
# Synonyms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header, part",
    [("Qty", "activity_value"), ("Units consumed (kWh)", "activity_value"), ("UOM", "activity_unit"), ("Bill date", "period"), ("Fuel type", "activity_name"), ("Plant", "site"), ("Emission category", "activity_name")],
)
def test_part_of_header(header, part):
    assert part in part_of_header(header)


def test_unknown_header_means_nothing():
    assert part_of_header("Remarks") == [] and part_of_header("") == []
