"""Deleting an invoice must keep its file while ESG-lite evidence links to it.

ESG-lite (B8) attaches an invoice's Cloudinary file to emission entries as
emission_document rows with ai_invoice_id; both point at the same file.
"""
import asyncio

INVOICE_TABLE = """
CREATE TABLE IF NOT EXISTS invoice (
  invoice_id serial PRIMARY KEY,
  file_name varchar NOT NULL,
  cloudinary_url varchar NOT NULL,
  cloudinary_public_id varchar NOT NULL,
  file_type varchar,
  file_size integer,
  uploaded_by integer,
  site_id integer,
  category_id integer,
  ocr_text jsonb,
  created_at timestamp NOT NULL DEFAULT now(),
  updated_at timestamp NOT NULL DEFAULT now()
)
"""


def _seed(cur):
    cur.execute(INVOICE_TABLE)
    cur.execute(
        """
        INSERT INTO invoice (file_name, cloudinary_url, cloudinary_public_id) VALUES
          ('linked.pdf', 'u1', 'invoices/linked'),
          ('free.pdf', 'u2', 'invoices/free'),
          ('linked2.pdf', 'u3', 'invoices/linked2'),
          ('free2.pdf', 'u4', 'invoices/free2')
        RETURNING invoice_id
        """
    )
    ids = [r[0] for r in cur.fetchall()]
    for invoice_id in (ids[0], ids[2]):
        cur.execute(
            """
            INSERT INTO emission_document
              (file_name, original_name, cloudinary_public_id, cloudinary_url, file_type, document_type, ai_invoice_id)
            VALUES ('f', 'f', 'p', 'u', 'application/pdf', 'invoice', %s)
            """,
            (invoice_id,),
        )
    return ids


def test_invoice_file_kept_while_linked(throwaway_db, monkeypatch):
    from app.api import invoices
    from app.core import cloudinary_service, database
    from app.core.auth import Principal

    superadmin = Principal(kind="user", user_id=1, role="Superadmin", site_ids=None)

    with throwaway_db.cursor() as cur:
        linked, free, linked2, free2 = _seed(cur)

    assert database.invoice_ids_with_documents([linked, free, linked2, free2]) == {linked, linked2}
    assert database.invoice_ids_with_documents([]) == set()

    destroyed = []
    monkeypatch.setattr(cloudinary_service, "delete_file", lambda public_id: destroyed.append(public_id))

    res = asyncio.run(invoices.delete_invoice(linked, superadmin))
    assert res["file_kept"] is True
    res = asyncio.run(invoices.delete_invoice(free, superadmin))
    assert res["file_kept"] is False
    assert destroyed == ["invoices/free"]

    res = asyncio.run(invoices.bulk_delete_invoices(invoices.BulkDeleteRequest(ids=[linked2, free2]), superadmin))
    assert res["deleted"] == 2 and res["files_kept"] == 1
    assert destroyed == ["invoices/free", "invoices/free2"]

    with throwaway_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM invoice")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM emission_document WHERE ai_invoice_id IS NOT NULL")
        assert cur.fetchone()[0] == 2  # evidence rows untouched


def test_delete_waits_for_a_link_in_progress(throwaway_db, monkeypatch):
    """A link added while the delete runs is seen: the delete locks the
    invoice row and checks links in the same transaction, and ESG-lite holds
    the row FOR SHARE while it links."""
    import os
    import threading

    import psycopg2

    from app.api import invoices
    from app.core import cloudinary_service
    from app.core.auth import Principal

    superadmin = Principal(kind="user", user_id=1, role="Superadmin", site_ids=None)

    with throwaway_db.cursor() as cur:
        cur.execute(INVOICE_TABLE)
        cur.execute(
            "INSERT INTO invoice (file_name, cloudinary_url, cloudinary_public_id)"
            " VALUES ('racing.pdf', 'u5', 'invoices/racing') RETURNING invoice_id"
        )
        invoice_id = cur.fetchone()[0]

    destroyed = []
    monkeypatch.setattr(cloudinary_service, "delete_file", lambda public_id: destroyed.append(public_id))

    linker = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USERNAME", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ["DB_NAME"],
    )
    try:
        with linker.cursor() as cur:
            # What ESG-lite's POST /user/documents/from-invoice does.
            cur.execute("SELECT invoice_id FROM invoice WHERE invoice_id = %s FOR SHARE", (invoice_id,))
            cur.execute(
                """
                INSERT INTO emission_document
                  (file_name, original_name, cloudinary_public_id, cloudinary_url, file_type, document_type, ai_invoice_id)
                VALUES ('f', 'f', 'invoices/racing', 'u5', 'application/pdf', 'invoice', %s)
                """,
                (invoice_id,),
            )

        result = {}
        worker = threading.Thread(target=lambda: result.update(asyncio.run(invoices.delete_invoice(invoice_id, superadmin))))
        worker.start()
        worker.join(timeout=1.5)
        assert worker.is_alive(), "delete must wait for the link transaction"
        linker.commit()
        worker.join(timeout=10)
        assert not worker.is_alive()
    finally:
        linker.rollback()
        linker.close()

    assert result["file_kept"] is True
    assert destroyed == []
