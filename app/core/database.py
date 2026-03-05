
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from app.core.config import settings

logger = logging.getLogger(__name__)

def get_connection():
    """Get a database connection to emissions_db."""
    return psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USERNAME,
        password=settings.DB_PASSWORD,
        dbname=settings.DB_NAME,
    )


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
        conn.close()


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
        conn.close()


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
        conn.close()


def get_invoice_by_id(invoice_id: int) -> dict | None:
    """Get a single invoice by ID."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM invoice WHERE invoice_id = %s", (invoice_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()
