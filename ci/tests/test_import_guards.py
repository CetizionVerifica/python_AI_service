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
