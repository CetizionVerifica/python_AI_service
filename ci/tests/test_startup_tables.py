"""The tables this service creates on startup must never lose rows on restart."""


def test_startup_ddl_is_idempotent_and_keeps_rows(throwaway_db):
    from app.core.database import ensure_emission_factor_uploads_table, ensure_uploaded_documents_table

    ensure_emission_factor_uploads_table()
    ensure_uploaded_documents_table()
    with throwaway_db.cursor() as cur:
        cur.execute(
            "INSERT INTO emission_factor_uploads (file_name, cloudinary_url, cloudinary_public_id) VALUES ('a.xlsx', 'u', 'p')"
        )
        cur.execute(
            "INSERT INTO uploaded_documents (document_name, cloudinary_url) VALUES ('b.csv', 'u')"
        )

    # Second boot: same DDL again, existing rows must survive.
    ensure_emission_factor_uploads_table()
    ensure_uploaded_documents_table()
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM emission_factor_uploads")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM uploaded_documents")
        assert cur.fetchone()[0] == 1
