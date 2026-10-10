"""Category lookups only ever use the caller's own company's data (F-08).

Company 1 (reference data) owns sites 1 and 2. Added here: company 60 with
site 60, holding a site-less mapping and its own factor names. Nothing of
company 60 may resolve a name for a site of company 1, and vice versa.
"""
import pytest


@pytest.fixture
def other_company(throwaway_db):
    with throwaway_db.cursor() as cur:
        cur.execute("INSERT INTO company (company_id, name, address, contact_person) VALUES (60, 'Rival Co', 'x', 'x')")
        cur.execute("INSERT INTO site (site_id, name, address, contact_person, company_id) VALUES (60, 'Rival plant', 'x', 'x', 60)")
        cur.execute(
            # Explicit ids: the import snapshot test records factor ids from the sequence.
            "INSERT INTO emission_factors (emission_factor_id, site_id, category_id, year, factor_value, denominator_unit, source, emission_category_name) VALUES "
            "(601, 60, 1, 2024, 1.0000, 'litre', 'CI', 'Rival Secret Fuel'),"
            "(602, NULL, 5, 2024, 1.0000, 'tonne', 'CI', 'Shared Landfill')"
        )
        cur.execute(
            "INSERT INTO emission_category_mapping (id, company_id, company_name, site_id, category_id, company_category_name, global_category_name) VALUES "
            "(601, 60, 'Rival Co', NULL, 1, 'HSD', 'Rival Secret Fuel'),"
            "(602, 1, 'CI Steel Co', NULL, 1, 'Gasoil', 'Diesel')"
        )
    yield
    with throwaway_db.cursor() as cur:
        cur.execute("DELETE FROM emission_category_mapping WHERE id IN (601, 602)")
        cur.execute("DELETE FROM emission_factors WHERE emission_factor_id IN (601, 602)")
        cur.execute("DELETE FROM site WHERE site_id = 60")
        cur.execute("DELETE FROM company WHERE company_id = 60")


def test_mapping_fallback_ignores_other_companies(other_company):
    from app.services import excel_parser as ep

    resolver = ep._build_category_resolver(site_id=1, category_id=1)
    assert resolver == {"gasoil": "Diesel"}
    assert ep._build_category_resolver(site_id=60, category_id=1) == {"hsd": "Rival Secret Fuel"}


def test_category_fallback_stays_in_company(other_company):
    from app.services import category_matcher as cm

    names = {c["emission_category_name"] for c in cm.fetch_emission_categories(site_id=2)}
    assert "Rival Secret Fuel" not in names
    assert {"Diesel", "Landfill", "Shared Landfill"} <= names  # company sites + shared library

    # Site 2 has no category-1 factor named like this; the fallback must not
    # reach company 60's factor names.
    match = cm.match_category("Rival Secret Fuel", site_id=2, category_id=5)
    assert match is None or match.emission_category_name != "Rival Secret Fuel"

    # No site: only the shared library.
    assert {c["emission_category_name"] for c in cm.fetch_emission_categories()} == {"Shared Landfill"}
    # Superadmin listing of every company stays available on request.
    every = {c["emission_category_name"] for c in cm.fetch_emission_categories(all_companies=True)}
    assert "Rival Secret Fuel" in every
