
import logging
import json
import psycopg2
from psycopg2 import pool as _pg_pool
from psycopg2.extras import RealDictCursor, execute_values, Json
from app.core.config import settings


logger = logging.getLogger(__name__)

EMISSION_TABLE = "emission"  # change if your table name differs

# ---------------------------------------------------------------------------
# Connection pool (reuses TCP connections instead of creating new ones)
# ---------------------------------------------------------------------------
_pool: _pg_pool.ThreadedConnectionPool | None = None


def _get_pool() -> _pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        try:
            _pool = _pg_pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=20,
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USERNAME,
                password=settings.DB_PASSWORD,
                dbname=settings.DB_NAME,
            )
        except psycopg2.OperationalError as exc:
            if not settings.DB_PASSWORD:
                raise RuntimeError(
                    "Could not connect to Postgres and DB_PASSWORD is not set. "
                    "Set DB_PASSWORD (and DB_HOST/DB_USERNAME/DB_NAME) in the environment."
                ) from exc
            raise
    return _pool


def get_connection():
    """Get a live database connection from the pool.

    Pooled connections can die underneath us (Postgres restart, an admin
    terminating sessions, idle timeouts). Handing such a connection out makes
    every request fail with "connection already closed" until the service is
    restarted, so probe each connection and replace dead ones.
    """
    pool = _get_pool()
    for _ in range(pool.maxconn + 1):
        conn = pool.getconn()
        if conn.closed:
            pool.putconn(conn, close=True)
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return conn
        except Exception:
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
    return pool.getconn()


def release_connection(conn):
    """Return a connection to the pool instead of closing it."""
    try:
        if conn.closed:
            _get_pool().putconn(conn, close=True)
            return
        # Always rollback any open transaction before returning to pool.
        # This ensures the next user of this connection gets a clean state.
        # (Calling rollback after commit is harmless — it's a no-op.)
        conn.rollback()
        _get_pool().putconn(conn)
    except Exception:
        # If pool is closed or conn is bad, just close it directly
        try:
            conn.close()
        except Exception:
            pass


def insert_invoice(
    file_name: str,
    cloudinary_url: str,
    cloudinary_public_id: str,
    file_type: str,
    file_size: int | None,
    uploaded_by: int | None = None,
    site_id: int | None = None,
    category_id: int | None = None,
) -> dict:
    """Insert a new invoice record and return it."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO invoice (
                    file_name, cloudinary_url, cloudinary_public_id,
                    file_type, file_size, uploaded_by, site_id, category_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    file_name, cloudinary_url, cloudinary_public_id,
                    file_type, file_size, uploaded_by, site_id, category_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row)
    except Exception as e:
        conn.rollback()
        logger.error(f"DB insert_invoice failed: {e}")
        raise
    finally:
        release_connection(conn)


def update_invoice_ocr(invoice_id: int, ocr_text: dict) -> dict:
    """Update the ocr_text field for an invoice."""
    import json
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE invoice
                SET ocr_text = %s, updated_at = NOW()
                WHERE invoice_id = %s
                RETURNING *
                """,
                (json.dumps(ocr_text), invoice_id),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    except Exception as e:
        conn.rollback()
        logger.error(f"DB update_invoice_ocr failed: {e}")
        raise
    finally:
        release_connection(conn)


def get_invoices(
    site_id: int | None = None,
    category_id: int | None = None,
    uploaded_by: int | None = None,
    *,
    scope_site_ids: list[int] | None = None,
    scope_user_id: int | None = None,
) -> list:
    """Get invoices with optional filters.

    scope_site_ids limits the result to those sites plus the invoices without
    a site that scope_user_id uploaded; None means no limit (Superadmin).
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = "SELECT * FROM invoice WHERE 1=1"
            params = []
            if scope_site_ids is not None:
                query += " AND (site_id = ANY(%s) OR (site_id IS NULL AND uploaded_by = %s))"
                params.extend([list(scope_site_ids), scope_user_id])
            if site_id:
                query += " AND site_id = %s"
                params.append(site_id)
            if category_id:
                query += " AND category_id = %s"
                params.append(category_id)
            if uploaded_by:
                query += " AND uploaded_by = %s"
                params.append(uploaded_by)
            query += " ORDER BY created_at DESC"
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        release_connection(conn)


def get_invoice_by_id(invoice_id: int) -> dict | None:
    """Get a single invoice by ID."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM invoice WHERE invoice_id = %s", (invoice_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        release_connection(conn)


def delete_invoice(invoice_id: int) -> dict | None:
    """Delete an invoice and return the deleted record (for Cloudinary cleanup).

    The record carries ``file_linked``: True when ESG-lite evidence documents
    still use the invoice's file, which must then be kept. The link check runs
    in the same transaction as the delete, with the invoice row locked
    (FOR UPDATE), so a link being added at the same moment (ESG-lite holds
    the row FOR SHARE while it links) is either seen here or refused there.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT invoice_id FROM invoice WHERE invoice_id = %s FOR UPDATE",
                (invoice_id,),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return None
            linked = _linked_invoice_ids(cur, [invoice_id])
            cur.execute(
                "DELETE FROM invoice WHERE invoice_id = %s RETURNING *",
                (invoice_id,),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                return None
            return {**dict(row), "file_linked": invoice_id in linked}
    except Exception as e:
        conn.rollback()
        logger.error(f"DB delete_invoice failed: {e}")
        raise
    finally:
        release_connection(conn)


def bulk_delete_invoices(invoice_ids: list[int]) -> list[dict]:
    """Bulk delete invoices and return deleted records.

    Each record carries ``file_linked`` (see delete_invoice); rows are locked
    in id order before the link check, all in one transaction.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT invoice_id FROM invoice WHERE invoice_id = ANY(%s) ORDER BY invoice_id FOR UPDATE",
                (invoice_ids,),
            )
            locked = [r["invoice_id"] for r in cur.fetchall()]
            linked = _linked_invoice_ids(cur, locked)
            cur.execute(
                "DELETE FROM invoice WHERE invoice_id = ANY(%s) RETURNING *",
                (locked,),
            )
            rows = cur.fetchall()
            conn.commit()
            return [{**dict(row), "file_linked": row["invoice_id"] in linked} for row in rows]
    except Exception as e:
        conn.rollback()
        logger.error(f"DB bulk_delete_invoices failed: {e}")
        raise
    finally:
        release_connection(conn)


def invoice_ids_with_documents(invoice_ids: list[int]) -> set[int]:
    """Invoice ids that ESG-lite evidence documents still point at.

    ESG-lite's POST /user/documents/from-invoice attaches an invoice's
    Cloudinary file to emission entries (emission_document.ai_invoice_id),
    so those files must outlive the invoice row. Returns an empty set when
    the column does not exist yet (ESG-lite migrate:document-ai-invoice not
    run), which keeps today's behaviour.
    """
    if not invoice_ids:
        return set()
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return _linked_invoice_ids(cur, invoice_ids)
    finally:
        release_connection(conn)


def _linked_invoice_ids(cur, invoice_ids: list[int]) -> set[int]:
    """invoice_ids_with_documents on an open RealDictCursor (same transaction)."""
    if not invoice_ids:
        return set()
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'emission_document' AND column_name = 'ai_invoice_id'
        """
    )
    if cur.fetchone() is None:
        return set()
    cur.execute(
        "SELECT DISTINCT ai_invoice_id FROM emission_document WHERE ai_invoice_id = ANY(%s)",
        (list(invoice_ids),),
    )
    return {row["ai_invoice_id"] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Emission Factor Uploads table
# ---------------------------------------------------------------------------

def ensure_emission_factor_uploads_table():
    """Create the emission_factor_uploads table if it does not exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS emission_factor_uploads (
                    id SERIAL PRIMARY KEY,
                    file_name VARCHAR(500) NOT NULL,
                    cloudinary_url TEXT NOT NULL,
                    cloudinary_public_id VARCHAR(500) NOT NULL,
                    file_size INTEGER,
                    uploaded_by INTEGER,
                    site_id INTEGER,
                    category_ids INTEGER[],
                    layout_type VARCHAR(100),
                    total_records INTEGER DEFAULT 0,
                    records_created INTEGER DEFAULT 0,
                    records_skipped INTEGER DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'parsed',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
            logger.info("Ensured emission_factor_uploads table exists")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create emission_factor_uploads table: {e}")
        raise
    finally:
        release_connection(conn)


def insert_emission_factor_upload(
    file_name: str,
    cloudinary_url: str,
    cloudinary_public_id: str,
    file_size: int | None = None,
    uploaded_by: int | None = None,
    site_id: int | None = None,
    category_ids: list[int] | None = None,
    layout_type: str | None = None,
    total_records: int = 0,
) -> dict:
    """Insert a new emission factor upload record."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO emission_factor_uploads (
                    file_name, cloudinary_url, cloudinary_public_id,
                    file_size, uploaded_by, site_id, category_ids,
                    layout_type, total_records
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    file_name, cloudinary_url, cloudinary_public_id,
                    file_size, uploaded_by, site_id, category_ids,
                    layout_type, total_records,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row)
    except Exception as e:
        conn.rollback()
        logger.error(f"DB insert_emission_factor_upload failed: {e}")
        raise
    finally:
        release_connection(conn)


def update_emission_factor_upload_results(
    upload_id: int,
    records_created: int,
    records_skipped: int,
    status: str = "completed",
) -> dict | None:
    """Update an upload record with final results after bulk create."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE emission_factor_uploads
                SET records_created = %s,
                    records_skipped = %s,
                    status = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (records_created, records_skipped, status, upload_id),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    except Exception as e:
        conn.rollback()
        logger.error(f"DB update_emission_factor_upload_results failed: {e}")
        raise
    finally:
        release_connection(conn)


def get_emission_factor_uploads(uploaded_by: int | None = None) -> list:
    """Get all emission factor uploads, newest first."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = "SELECT * FROM emission_factor_uploads WHERE 1=1"
            params = []
            if uploaded_by:
                query += " AND uploaded_by = %s"
                params.append(uploaded_by)
            query += " ORDER BY created_at DESC"
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        release_connection(conn)


def delete_emission_factor_upload(upload_id: int) -> dict | None:
    """Delete an upload record and return it (for Cloudinary cleanup)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "DELETE FROM emission_factor_uploads WHERE id = %s RETURNING *",
                (upload_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    except Exception as e:
        conn.rollback()
        logger.error(f"DB delete_emission_factor_upload failed: {e}")
        raise
    finally:
        release_connection(conn)


def bulk_delete_emission_factor_uploads(upload_ids: list[int]) -> list[dict]:
    """Bulk delete upload records and return deleted rows (for Cloudinary cleanup)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "DELETE FROM emission_factor_uploads WHERE id = ANY(%s) RETURNING *",
                (upload_ids,),
            )
            rows = cur.fetchall()
            conn.commit()
            return [dict(row) for row in rows]
    except Exception as e:
        conn.rollback()
        logger.error(f"DB bulk_delete_emission_factor_uploads failed: {e}")
        raise
    finally:
        release_connection(conn)


def fetch_column_config(site_id: int, category_id: int) -> dict | None:
    """
    Fetch column_config with associated column details for a site+category.
    Returns the config with columns, dropdown options, dependencies, and
    emission_category_mapping — or None if no config exists.
    """
    import json as _json
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Fetch the column_config row
            # ORDER BY config_name mirrors the entry form and the Node backend,
            # which both use configs[0] of the name-sorted list — all three
            # engines must read one and the same config.
            cur.execute(
                """
                SELECT pk_id, config_name, column_options, column_dependencies,
                       dependent_options, emission_category_mapping, calculation
                FROM column_config
                WHERE site_id = %s AND category_id = %s
                ORDER BY config_name ASC
                LIMIT 1
                """,
                (site_id, category_id),
            )
            config_row = cur.fetchone()
            if not config_row:
                return None

            config = dict(config_row)
            config_id = config["pk_id"]

            # Parse JSONB fields that may come back as strings
            # (calculation stays None when the column is null — callers treat
            # None as "normal one-value category")
            for field in ("column_options", "column_dependencies", "dependent_options", "emission_category_mapping", "calculation"):
                val = config.get(field)
                if isinstance(val, str):
                    try:
                        config[field] = _json.loads(val)
                    except _json.JSONDecodeError:
                        config[field] = {}
                elif val is None:
                    config[field] = {}

            # 2. Fetch associated columns via the join table
            cur.execute(
                """
                SELECT ce.pk_id, ce.column_name, ce.column_type
                FROM column_config_columns ccc
                JOIN column_entity ce ON ce.pk_id = ccc.column_id
                WHERE ccc.column_config_id = %s
                ORDER BY ce.pk_id
                """,
                (config_id,),
            )
            columns = [dict(row) for row in cur.fetchall()]
            config["columns"] = columns

            return config
    except Exception as e:
        logger.warning(f"fetch_column_config failed for site={site_id}, category={category_id}: {e}")
        return None
    finally:
        release_connection(conn)

def ensure_uploaded_documents_table():
    """
    Creates the uploaded_documents table (once).
    Call this on FastAPI startup.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS uploaded_documents (
                    id BIGSERIAL PRIMARY KEY,
                    document_name TEXT NOT NULL,
                    cloudinary_url TEXT NOT NULL,
                    cloudinary_public_id TEXT,
                    public_url TEXT,
                    file_type TEXT,
                    file_size BIGINT,
                    total_rows BIGINT,
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_uploaded_documents_created_at ON uploaded_documents(created_at DESC);"
            )

            # Who uploaded the document; the import wizard only lets that
            # user (or a Superadmin) read it back. NULL on older rows.
            cur.execute("ALTER TABLE uploaded_documents ADD COLUMN IF NOT EXISTS uploaded_by INTEGER;")

            conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"DB ensure_uploaded_documents_table failed: {e}")
        raise
    finally:
        release_connection(conn)


def insert_uploaded_document(
    document_name: str,
    cloudinary_url: str,
    cloudinary_public_id: str | None = None,
    public_url: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    uploaded_by: int | None = None,
) -> dict:
    """
    Insert an uploaded document record and return the inserted row (includes id).
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO uploaded_documents (
                    document_name,
                    cloudinary_url,
                    cloudinary_public_id,
                    public_url,
                    file_type,
                    file_size,
                    uploaded_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    document_name,
                    cloudinary_url,
                    cloudinary_public_id,
                    public_url,
                    file_type,
                    file_size,
                    uploaded_by,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row)
    except Exception as e:
        conn.rollback()
        logger.error(f"DB insert_uploaded_document failed: {e}")
        raise
    finally:
        release_connection(conn)


def get_uploaded_document_by_id(document_id: int) -> dict | None:
    """
    Fetch a single uploaded document by id.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM uploaded_documents WHERE id = %s", (document_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        release_connection(conn)


def update_uploaded_document(
    document_id: int,
    *,
    status: str | None = None,
    total_rows: int | None = None,
) -> dict | None:
    """
    Update status/total_rows and return updated row.
    """
    fields = []
    params = []

    if status is not None:
        fields.append("status = %s")
        params.append(status)

    if total_rows is not None:
        fields.append("total_rows = %s")
        params.append(total_rows)

    # Always touch updated_at if anything changes
    if not fields:
        return get_uploaded_document_by_id(document_id)

    fields.append("updated_at = NOW()")
    params.append(document_id)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE uploaded_documents
                SET {", ".join(fields)}
                WHERE id = %s
                RETURNING *
                """,
                tuple(params),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    except Exception as e:
        conn.rollback()
        logger.error(f"DB update_uploaded_document failed: {e}")
        raise
    finally:
        release_connection(conn)


IMPORT_CLAIM_STALE_MINUTES = 30


def claim_uploaded_document_for_import(document_id: int) -> tuple[bool, str | None]:
    """
    Atomically mark a document 'importing' so a second import of the same file
    (double click, retry, parallel request) is refused. A claim older than
    IMPORT_CLAIM_STALE_MINUTES is treated as left over from a crashed worker.

    Returns (claimed, current_status); current_status is None when the
    document does not exist.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE uploaded_documents
                SET status = 'importing', updated_at = NOW()
                WHERE id = %s
                  AND (status NOT IN ('importing', 'imported', 'deleted')
                       OR (status = 'importing'
                           AND updated_at < NOW() - (%s * INTERVAL '1 minute')))
                RETURNING id
                """,
                (document_id, IMPORT_CLAIM_STALE_MINUTES),
            )
            claimed = cur.fetchone() is not None
            conn.commit()
            if claimed:
                return True, "importing"
            cur.execute("SELECT status FROM uploaded_documents WHERE id = %s", (document_id,))
            row = cur.fetchone()
            return False, (row[0] if row else None)
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def mark_document_imported_with_conn(conn, document_id: int) -> bool:
    """
    Inside the import's own transaction: flip 'importing' to 'imported'. False
    when another import already finished the same document, in which case the
    caller must roll back. The row lock makes a parallel import wait here and
    then see 'imported'.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE uploaded_documents SET status = 'imported', updated_at = NOW()
            WHERE id = %s AND status = 'importing'
            """,
            (document_id,),
        )
        return cur.rowcount == 1


def release_import_claim(document_id: int) -> None:
    """A failed import hands the document back so it can be imported again."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE uploaded_documents SET status = 'uploaded', updated_at = NOW()
                WHERE id = %s AND status = 'importing'
                """,
                (document_id,),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        logger.warning(f"Could not release the import claim on document_id={document_id}", exc_info=True)
    finally:
        release_connection(conn)


def list_documents_for_cleanup(older_than_days: int = 7, limit: int = 500) -> list[dict]:
    """
    Documents whose stored file can be reclaimed: older than the cutoff and not
    already cleaned up. The age cutoff is what keeps an in-progress upload
    wizard from having its file pulled out from under it.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, document_name, cloudinary_url, cloudinary_public_id, status
                FROM uploaded_documents
                WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                  AND status NOT IN ('deleted', 'importing')
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (older_than_days, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"DB list_documents_for_cleanup failed: {e}")
        raise
    finally:
        release_connection(conn)


def get_emission_factor(
    site_id: int,
    category_id: int,
    year: int,
    emission_category_name: str,
) -> dict | None:
    """
    Tries (site_id, category_id, year, emission_category_name).
    If not found -> fallback (site_id, category_id, emission_category_name).
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM emission_factors
                WHERE site_id = %s
                  AND category_id = %s
                  AND emission_category_name = %s
                  AND year = %s
                LIMIT 1
                """,
                (site_id, category_id, emission_category_name, year),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            cur.execute(
                """
                SELECT *
                FROM emission_factors
                WHERE site_id = %s
                  AND category_id = %s
                  AND emission_category_name = %s
                ORDER BY year DESC
                LIMIT 1
                """,
                (site_id, category_id, emission_category_name),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        release_connection(conn)



def bulk_insert_emissions(rows: list[dict], page_size: int = 2000) -> int:
    """
    Inserts emissions in bulk with execute_values (fast).
    Expected keys per row:
      site_id, category_id, activity_data (dict), total_emission (float),
      unit (str), date_of_reporting (str or date), activity_data_unit (str|None),
      created_by (int|None)
    """
    if not rows:
        return 0

    values = []
    for r in rows:
        ef_snapshot = r.get("emission_factor_snapshot")
        extra_data = r.get("extra_data") or {}
        values.append(
            (
                r["site_id"],
                r["category_id"],
                json.dumps(r["activity_data"]),
                json.dumps(extra_data),
                r["total_emission"],
                r.get("unit") or "tCO2e",
                r["date_of_reporting"],
                r.get("activity_data_unit"),
                r.get("created_by"),
                r.get("upload_batch_id"),
                json.dumps(ef_snapshot) if isinstance(ef_snapshot, dict) else None,
            )
        )

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"""
                INSERT INTO {EMISSION_TABLE}
                  (site_id, category_id, activity_data, extra_data, total_emission, unit,
                   date_of_reporting, activity_data_unit, created_by, upload_batch_id,
                   emission_factor_snapshot)
                VALUES %s
                """,
                values,
                page_size=page_size,
            )
            inserted = len(values)
            conn.commit()
            return inserted
    except Exception as e:
        conn.rollback()
        logger.error(f"DB bulk_insert_emissions failed: {e}")
        raise
    finally:
        release_connection(conn)


def fetch_emission_factor(
    conn,
    site_id: int,
    category_id: int,
    year: int,
    emission_category_name: str,
) -> dict | None:
    """
    Fetch emission factor using an existing connection (for shared transactions).
    Tries exact year first, then falls back to any year (latest).
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT factor_value, denominator_unit
            FROM emission_factors
            WHERE site_id = %s
              AND category_id = %s
              AND year = %s
              AND emission_category_name = %s
            LIMIT 1
            """,
            (site_id, category_id, year, emission_category_name),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

        # fallback without year (same as Node fallback)
        cur.execute(
            """
            SELECT factor_value, denominator_unit
            FROM emission_factors
            WHERE site_id = %s
              AND category_id = %s
              AND emission_category_name = %s
            LIMIT 1
            """,
            (site_id, category_id, emission_category_name),
        )
        row = cur.fetchone()
        return dict(row) if row else None



def bulk_insert_emissions_with_conn(conn, rows: list[dict], returning: bool = False):
    """
    Insert many rows into emission table using an existing connection.
    Caller commits/rollbacks. Returns the row count, or with ``returning=True``
    the new pk_ids in the order of ``rows``.
    """
    if not rows:
        return [] if returning else 0

    values = []
    for r in rows:
        activity_data = r["activity_data"]
        extra_data = r.get("extra_data") or {}
        ef_snapshot = r.get("emission_factor_snapshot")
        values.append(
            (
                r["site_id"],
                r["category_id"],
                Json(activity_data) if isinstance(activity_data, dict) else activity_data,
                Json(extra_data) if isinstance(extra_data, dict) else extra_data,
                r["total_emission"],
                r.get("unit") or "tCO2e",
                r["date_of_reporting"],
                r.get("activity_data_unit"),
                r.get("created_by"),
                r.get("upload_batch_id"),
                Json(ef_snapshot) if isinstance(ef_snapshot, dict) else None,
                r.get("fera_linked_id"),
            )
        )

    with conn.cursor() as cur:
        result = execute_values(
            cur,
            f"""
            INSERT INTO {EMISSION_TABLE}
              (site_id, category_id, activity_data, extra_data, total_emission, unit,
               date_of_reporting, activity_data_unit, created_by, upload_batch_id,
               emission_factor_snapshot, fera_linked_id)
            VALUES %s
            {"RETURNING pk_id" if returning else ""}
            """,
            values,
            page_size=2000,
            fetch=returning,
        )

    if returning:
        return [r[0] for r in result]
    return len(values)


# Advisory-lock namespace for "imports into one site": two imports of different
# files for the same site run one after the other, so both duplicate checks see
# the other's rows.
IMPORT_SITE_LOCK = 2027


def lock_site_for_import_with_conn(conn, site_id: int) -> None:
    """Held until the import's transaction ends."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (IMPORT_SITE_LOCK, site_id))


def monthly_activity_in_month_with_conn(conn, site_id: int, category_id: int, day: str, exclude_batch: str) -> list[dict]:
    """
    activity_data of the monthly entries already saved for this site and
    category in the month of `day`, leaving out the rows of the import that is
    running. Matched by month, not by date: the form files a month on its last
    day, while a sheet row keeps the day written in it.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT activity_data FROM {EMISSION_TABLE}
            WHERE site_id = %s AND category_id = %s
              AND date_trunc('month', date_of_reporting) = date_trunc('month', %s::date)
              AND reporting_period = 'monthly' AND upload_batch_id IS DISTINCT FROM %s
            """,
            (site_id, category_id, day, exclude_batch),
        )
        return [r[0] if isinstance(r[0], dict) else {} for r in cur.fetchall()]


def yearly_covers_date_with_conn(conn, site_id: int, category_id: int, day: str) -> bool:
    """
    True when a yearly entry for this site and category covers the date (the
    mode lock). Same window as ESG-lite's yearlyCoversDateSql: CY is the
    calendar year of its period end, FY the twelve months ending on it.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT 1 FROM {EMISSION_TABLE} e
            WHERE e.site_id = %(site)s AND e.category_id = %(cat)s AND e.reporting_period = 'yearly'
              AND ((e.year_type = 'CY'
                    AND EXTRACT(YEAR FROM e.date_of_reporting) = EXTRACT(YEAR FROM %(day)s::date))
                OR (e.year_type = 'FY'
                    AND %(day)s::date > e.date_of_reporting - INTERVAL '1 year'
                    AND %(day)s::date <= e.date_of_reporting))
            LIMIT 1
            """,
            {"site": site_id, "cat": category_id, "day": day},
        )
        return cur.fetchone() is not None


def audit_imported_rows_with_conn(conn, rows: list[tuple[int, float]], user_id: int | None, reason: str) -> None:
    """
    One audit_log row per imported entry (pk_id, total), action "import", in
    the import's own transaction, so an entry's history shows where it came
    from. Skipped with a warning when ESG-lite's audit_log table is not
    there yet.
    """
    if not rows:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('audit_log') IS NOT NULL")
        if not cur.fetchone()[0]:
            logger.warning("audit_log table missing; bulk import not audited")
            return
        execute_values(
            cur,
            """
            INSERT INTO audit_log (entity_type, entity_id, action, changed_fields, reason, changed_by)
            VALUES %s
            """,
            [
                (
                    "emission", pk_id, "import",
                    Json({"status": {"old": None, "new": "pending"}, "total_emission": {"old": None, "new": f"{float(total):.2f}"}}),
                    reason, user_id,
                )
                for pk_id, total in rows
            ],
            page_size=2000,
        )


def link_fera_rows_with_conn(conn, pairs: list[tuple[int, int]]) -> None:
    """Point each source row at its auto-created FERA row: (source pk_id, FERA pk_id)."""
    if not pairs:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            UPDATE {EMISSION_TABLE} AS e SET fera_linked_id = v.fera_id
            FROM (VALUES %s) AS v(source_id, fera_id)
            WHERE e.pk_id = v.source_id
            """,
            pairs,
            page_size=2000,
        )
