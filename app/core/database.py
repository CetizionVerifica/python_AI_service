
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
_pool: _pg_pool.SimpleConnectionPool | None = None


def _get_pool() -> _pg_pool.SimpleConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = _pg_pool.SimpleConnectionPool(
            minconn=2,
            maxconn=10,
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USERNAME,
            password=settings.DB_PASSWORD,
            dbname=settings.DB_NAME,
        )
    return _pool


def get_connection():
    """Get a database connection from the pool."""
    return _get_pool().getconn()


def release_connection(conn):
    """Return a connection to the pool instead of closing it."""
    try:
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


def get_invoices(site_id: int | None = None, category_id: int | None = None, uploaded_by: int | None = None) -> list:
    """Get all invoices with optional filters."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = "SELECT * FROM invoice WHERE 1=1"
            params = []
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
    """Delete an invoice and return the deleted record (for Cloudinary cleanup)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "DELETE FROM invoice WHERE invoice_id = %s RETURNING *",
                (invoice_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    except Exception as e:
        conn.rollback()
        logger.error(f"DB delete_invoice failed: {e}")
        raise
    finally:
        release_connection(conn)


def bulk_delete_invoices(invoice_ids: list[int]) -> list[dict]:
    """Bulk delete invoices and return deleted records."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "DELETE FROM invoice WHERE invoice_id = ANY(%s) RETURNING *",
                (invoice_ids,),
            )
            rows = cur.fetchall()
            conn.commit()
            return [dict(row) for row in rows]
    except Exception as e:
        conn.rollback()
        logger.error(f"DB bulk_delete_invoices failed: {e}")
        raise
    finally:
        release_connection(conn)


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
            cur.execute(
                """
                SELECT pk_id, config_name, column_options, column_dependencies,
                       dependent_options, emission_category_mapping
                FROM column_config
                WHERE site_id = %s AND category_id = %s
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
            for field in ("column_options", "column_dependencies", "dependent_options", "emission_category_mapping"):
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
                    file_size
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    document_name,
                    cloudinary_url,
                    cloudinary_public_id,
                    public_url,
                    file_type,
                    file_size,
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



def bulk_insert_emissions_with_conn(conn, rows: list[dict]) -> int:
    """
    Insert many rows into emission table using an existing connection.
    Caller commits/rollbacks.
    """
    if not rows:
        return 0

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
                r.get("upload_batch_id"),
                Json(ef_snapshot) if isinstance(ef_snapshot, dict) else None,
            )
        )

    with conn.cursor() as cur:
        execute_values(
            cur,
            f"""
            INSERT INTO {EMISSION_TABLE}
              (site_id, category_id, activity_data, extra_data, total_emission, unit,
               date_of_reporting, activity_data_unit, upload_batch_id, emission_factor_snapshot)
            VALUES %s
            """,
            values,
            page_size=2000,
        )

    return len(values)
