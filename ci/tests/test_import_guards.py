"""Bulk import guards (F-13): one import per uploaded file, every skipped row
reported with its reason, no zero-total rows saved, inserted <= total_rows,
and FERA rows linked both ways to the row that produced them."""
import threading

import pytest

CSV = (
    "Fuel,Qty,Unit\n"
    "Diesel,1000,litre\n"
    "Diesel,0,litre\n"
    "Unobtainium,10,litre\n"
)
MAPPINGS = {"emission_category": "Fuel", "activity_value": "Qty", "activity_data_unit": "Unit"}
DOC = 911


@pytest.fixture
def doc(throwaway_db, monkeypatch):
    from app.core.database import ensure_uploaded_documents_table
    from app.services import excel_parser as ep

    ensure_uploaded_documents_table()
    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: (CSV.encode(), "csv"))
    monkeypatch.setattr(ep, "delete_document_file", lambda *a, **k: None)
    with throwaway_db.cursor() as cur:
        cur.execute("INSERT INTO category (category_id, category_name, scope) VALUES (28, 'FERA', 'Scope 3') ON CONFLICT DO NOTHING RETURNING 1")
        added_fera_category = cur.fetchone() is not None
        cur.execute(
            "INSERT INTO emission_factors (emission_factor_id, site_id, category_id, year, factor_value, denominator_unit, source, emission_category_name) "
            "VALUES (911, 1, 28, 2024, 610.0000, 'litre', 'CI', 'Diesel')"
        )
        cur.execute("INSERT INTO uploaded_documents (id, document_name, cloudinary_url) VALUES (%s, 'g.csv', 'stub')", (DOC,))
    yield
    with throwaway_db.cursor() as cur:
        cur.execute("DELETE FROM emission WHERE upload_batch_id IS NOT NULL AND date_of_reporting = '2025-06-30'")
        cur.execute("DELETE FROM uploaded_documents WHERE id = %s", (DOC,))
        cur.execute("DELETE FROM emission_factors WHERE emission_factor_id = 911")
        if added_fera_category:
            cur.execute("DELETE FROM category WHERE category_id = 28")


def run_import():
    from app.services import excel_parser as ep

    return ep.import_all_rows(
        document_id=DOC, mappings=MAPPINGS, selected_categories=[],
        site_id=1, category_id=1, date_of_reporting="2025-06-30", user_id=1,
    )


def test_skips_are_reported_and_never_saved(doc, throwaway_db):
    result = run_import()
    assert result["inserted"] == 1 and result["fera_inserted"] == 1
    assert result["inserted"] + result["skipped"] + result["not_selected"] == result["total_rows"] == 3
    assert result["skipped_rows"] == [
        {"row": 2, "emission_category": "Diesel", "reason": "Missing or zero activity value"},
        {"row": 3, "emission_category": "Unobtainium", "reason": "No emission factor found for 'Unobtainium' (year 2024)"},
    ]
    with throwaway_db.cursor() as cur:
        cur.execute(
            "SELECT pk_id, category_id, fera_linked_id, total_emission FROM emission WHERE upload_batch_id = %s ORDER BY pk_id",
            (result["upload_batch_id"],),
        )
        (src_id, src_cat, src_link, src_total), (fera_id, fera_cat, fera_link, _) = cur.fetchall()
    assert (src_cat, fera_cat) == (1, 28)
    assert src_link == fera_id and fera_link == src_id
    assert float(src_total) > 0


def test_same_document_is_imported_once(doc):
    from app.services.excel_parser import ImportConflict

    run_import()
    with pytest.raises(ImportConflict):
        run_import()


def test_parallel_imports_insert_once(doc, throwaway_db):
    results, errors = [], []

    def worker():
        try:
            results.append(run_import())
        except Exception as e:  # noqa: BLE001 - collected for the assertion
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    from app.services.excel_parser import ImportConflict

    assert len(results) == 1
    assert len(errors) == 3 and all(isinstance(e, ImportConflict) for e in errors)
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM emission WHERE date_of_reporting = '2025-06-30' AND category_id = 1")
        assert cur.fetchone()[0] == 1


def test_failed_import_can_be_retried(doc, monkeypatch):
    from app.services import excel_parser as ep

    def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(ep, "bulk_insert_emissions_with_conn", boom)
    with pytest.raises(RuntimeError):
        run_import()
    monkeypatch.undo()
    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: (CSV.encode(), "csv"))
    monkeypatch.setattr(ep, "delete_document_file", lambda *a, **k: None)
    assert run_import()["inserted"] == 1


# P27-04: imported rows follow the form's rules (duplicate, mode lock, audit).

@pytest.fixture
def saved_entries(throwaway_db):
    """Entries saved by hand before the import; removed afterwards."""
    ids: list[int] = []

    def add(category_id, day, period="monthly", year_type=None, emission_category="Diesel"):
        with throwaway_db.cursor() as cur:
            cur.execute(
                "INSERT INTO emission (activity_data, total_emission, unit, date_of_reporting, reporting_period, year_type, site_id, category_id, created_by) "
                "VALUES (%s, 1, 'tCO2e', %s, %s, %s, 1, %s, 1) RETURNING pk_id",
                (f'{{"emission_category": "{emission_category}", "activity_value": "5"}}', day, period, year_type, category_id),
            )
            ids.append(cur.fetchone()[0])

    yield add
    with throwaway_db.cursor() as cur:
        cur.execute("DELETE FROM emission WHERE pk_id = ANY(%s)", (ids,))


def diesel_reasons(result):
    return [s["reason"] for s in result["skipped_rows"] if s["emission_category"] == "Diesel" and s["row"] == 1]


def test_month_already_entered_is_skipped(doc, saved_entries):
    saved_entries(1, "2025-06-30", emission_category=" diesel ")
    result = run_import()
    assert result["inserted"] == 0 and result["fera_inserted"] == 0
    assert diesel_reasons(result) == ["An entry for 'Diesel' in 2025-06 already exists"]


def test_dated_sheet_row_matches_a_hand_entry_in_its_month(doc, saved_entries, monkeypatch, throwaway_db):
    # The form files June on the 30th; the sheet row says 15/06/2025.
    from app.services import excel_parser as ep

    saved_entries(1, "2025-06-30")
    sheet = "Date,Fuel,Qty,Unit\n15/06/2025,Diesel,1000,litre\n15/07/2025,Diesel,1000,litre\n"
    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: (sheet.encode(), "csv"))
    try:
        result = ep.import_all_rows(
            document_id=DOC, mappings={**MAPPINGS, "date_of_reporting": "Date"}, selected_categories=[],
            site_id=1, category_id=1, date_of_reporting="2025-06-30", user_id=1,
        )
        assert result["inserted"] == 1 and result["skipped"] == 1
        assert result["skipped_rows"][0]["reason"] == "An entry for 'Diesel' in 2025-06 already exists"
    finally:
        with throwaway_db.cursor() as cur:
            cur.execute("DELETE FROM emission WHERE upload_batch_id IS NOT NULL AND date_of_reporting = '2025-07-15'")


def test_other_fuel_same_month_still_imports(doc, saved_entries):
    saved_entries(1, "2025-06-30", emission_category="Petrol")
    assert run_import()["inserted"] == 1


def test_month_inside_a_yearly_entry_is_skipped(doc, saved_entries):
    saved_entries(1, "2025-12-31", period="yearly", year_type="CY")
    result = run_import()
    assert result["inserted"] == 0
    assert diesel_reasons(result) == [
        "A yearly entry already covers 2025-06-30 for this category; delete it first or keep this category yearly"
    ]


def test_fy_window_is_twelve_months_to_its_end(doc, saved_entries):
    # FY ending Mar 2025 covers Apr 2024..Mar 2025, not June 2025.
    saved_entries(1, "2025-03-31", period="yearly", year_type="FY")
    assert run_import()["inserted"] == 1


def test_yearly_fera_entry_skips_only_the_twin(doc, saved_entries):
    saved_entries(28, "2025-12-31", period="yearly", year_type="CY")
    result = run_import()
    assert result["inserted"] == 1 and result["fera_inserted"] == 0


def test_rows_of_one_sheet_are_not_duplicates_of_each_other(doc, monkeypatch):
    from app.services import excel_parser as ep

    sheet = "Fuel,Qty,Unit\nDiesel,1000,litre\nDiesel,500,litre\nDiesel,250,litre\n"
    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: (sheet.encode(), "csv"))
    result = ep.import_all_rows(
        document_id=DOC, mappings=MAPPINGS, selected_categories=[],
        site_id=1, category_id=1, date_of_reporting="2025-06-30", user_id=1, chunk_size=1,
    )
    assert result["inserted"] == 3 and result["skipped"] == 0


def test_every_imported_entry_is_audited(doc, throwaway_db):
    result = run_import()
    with throwaway_db.cursor() as cur:
        cur.execute(
            "SELECT e.pk_id, a.action, a.changed_by, a.changed_fields->'status'->>'new', a.reason "
            "FROM emission e JOIN audit_log a ON a.entity_type = 'emission' AND a.entity_id = e.pk_id "
            "WHERE e.upload_batch_id = %s ORDER BY e.pk_id",
            (result["upload_batch_id"],),
        )
        rows = cur.fetchall()
        cur.execute("DELETE FROM audit_log WHERE reason = %s", (f"Bulk import (batch {result['upload_batch_id']})",))
    assert len(rows) == result["inserted"] + result["fera_inserted"] == 2
    assert all(r[1:4] == ("import", 1, "pending") for r in rows)
