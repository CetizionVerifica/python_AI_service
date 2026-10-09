"""Locks what a spreadsheet bulk import writes to the shared emission table.

Runs the real import_all_rows() against a throwaway copy of the ESG-lite
schema (ci/fixtures/esg_lite_schema.sql), so it also proves this service's
SQL still fits the tables the Node backend owns. Only the file download and
the Cloudinary clean-up are stubbed.
"""
import pytest

IMPORTS = [
    {
        "name": "stationary combustion with per-row dates and FERA",
        "site_id": 1, "category_id": 1,
        "csv": (
            "Fuel,Qty,Unit,Month\n"
            "Diesel,1000,litre,15.01.2025\n"
            "Diesel,500,gallon,15.02.2025\n"
            "Natural Gas,12.5,mwh,15.03.2025\n"
            "diesel ,2200,litre,2026-01-10\n"
            "Unobtainium,10,litre,15.04.2025\n"
            "Diesel,10,kg,15.05.2025\n"
        ),
        "mappings": {"emission_category": "Fuel", "activity_value": "Qty", "activity_data_unit": "Unit", "date_of_reporting": "Month"},
    },
    {
        "name": "use of sold products (per-method spec), bad rows skipped",
        "site_id": 1, "category_id": 3,
        "csv": (
            "Grid,Method,Product,Sold,Per use,Uses,Share,Unit\n"
            "India Grid,Direct energy,Fan X1,\"1,200\",0.75,3650,,kwh\n"
            "India Grid,Share of energy,Fan X2,800,2.5,,40,kwh\n"
            "India Grid,Direct energy,Fan X3,10,,5,,kwh\n"
            "India Grid,Share of energy,Fan X4,10,1,,140,kwh\n"
            "Mars Grid,Direct energy,Fan X5,1,1,1,,kwh\n"
        ),
        "mappings": {
            "emission_category": "Grid", "Calculation Method": "Method", "Product Name": "Product",
            "Units Sold": "Sold", "Energy per use": "Per use", "Lifetime uses": "Uses",
            "Share in use": "Share", "activity_data_unit": "Unit",
        },
    },
    {
        "name": "transport (per-unit spec)",
        "site_id": 1, "category_id": 4,
        "csv": (
            "Mode,Ref,Weight,Distance,Unit\n"
            "HGV Diesel [tonne.km],S-1,22.5,640,Tonne KM\n"
            "Van Diesel [km],S-2,,1180,km\n"
            "HGV Diesel [tonne.km],S-3,0,640,tonne.km\n"
        ),
        "mappings": {"emission_category": "Mode", "Shipment Ref": "Ref", "Weight": "Weight", "Distance travelled": "Distance", "activity_data_unit": "Unit"},
    },
]


@pytest.fixture
def stub_storage(monkeypatch):
    from app.services import excel_parser as ep

    files = {}
    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: (files[doc_id].encode(), "csv"))
    monkeypatch.setattr(ep, "delete_document_file", lambda *a, **k: None)
    return files


def test_bulk_import_results(throwaway_db, stub_storage, snapshot):
    from app.services import excel_parser as ep

    with throwaway_db.cursor() as cur:
        # FERA (category 28) factor so the auto-FERA row path runs too.
        cur.execute("INSERT INTO category (category_id, category_name, scope) VALUES (28, 'Fuel and Energy Related Activities', 'Scope 3')")
        cur.execute(
            "INSERT INTO emission_factors (site_id, category_id, year, factor_value, denominator_unit, source, emission_category_name) "
            "VALUES (1, 28, 2024, 610.0000, 'litre', 'CI', 'Diesel')"
        )
        cur.execute("DELETE FROM emission")  # this test owns the table in the throwaway DB

    summaries = []
    for doc_id, imp in enumerate(IMPORTS, start=1):
        stub_storage[doc_id] = imp["csv"]
        result = ep.import_all_rows(
            document_id=doc_id, mappings=imp["mappings"], selected_categories=[],
            site_id=imp["site_id"], category_id=imp["category_id"],
            date_of_reporting="2025-06-30", user_id=1,
        )
        summaries.append({"name": imp["name"], "inserted": result["inserted"], "skipped": result["skipped"], "total_rows": result["total_rows"]})

    with throwaway_db.cursor() as cur:
        cur.execute("""
            SELECT site_id, category_id, to_char(date_of_reporting, 'YYYY-MM-DD'),
                   total_emission::text, unit, activity_data_unit, status::text,
                   activity_data, emission_factor_snapshot
              FROM emission ORDER BY pk_id
        """)
        cols = ["site_id", "category_id", "date", "total_emission", "unit", "activity_data_unit", "status", "activity_data", "factor"]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    snapshot("bulk_import", {"imports": summaries, "stored_emissions": rows})
