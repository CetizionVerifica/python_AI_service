"""ESG-lite #191: emission, audit_log and notification ids are bigint.

Past the old integer limit (2,147,483,647) the import helpers still insert
entries, link their FERA rows and write their audit rows, and psycopg2 hands
the ids back as Python ints.
"""
from app.core import database as db

BIG = 3_000_000_000


def _sequence_state(cur, seq):
    cur.execute(f"SELECT last_value, is_called FROM {seq}")
    return cur.fetchone()


def test_fixture_matches_the_widened_schema(throwaway_db):
    with throwaway_db.cursor() as cur:
        cur.execute(
            """
            SELECT table_name || '.' || column_name, data_type FROM information_schema.columns
             WHERE table_schema = 'public' AND (table_name, column_name) IN (
               ('emission', 'pk_id'), ('emission', 'fera_linked_id'), ('emission_document', 'emission_id'),
               ('audit_log', 'id'), ('audit_log', 'entity_id'), ('notification', 'id'))
            """
        )
        assert dict(cur.fetchall()) == {
            "emission.pk_id": "bigint",
            "emission.fera_linked_id": "bigint",
            "emission_document.emission_id": "bigint",
            "audit_log.id": "bigint",
            "audit_log.entity_id": "bigint",
            "notification.id": "bigint",
        }
        cur.execute(
            """
            SELECT sequencename, data_type::text FROM pg_sequences
             WHERE sequencename IN ('emission_pk_id_seq', 'audit_log_id_seq', 'notification_id_seq')
            """
        )
        assert set(cur.fetchall()) == {
            ("emission_pk_id_seq", "bigint"),
            ("audit_log_id_seq", "bigint"),
            ("notification_id_seq", "bigint"),
        }


def test_import_helpers_work_with_ids_past_2_31(throwaway_db):
    conn = throwaway_db
    with conn.cursor() as cur:
        saved = {seq: _sequence_state(cur, seq) for seq in ("emission_pk_id_seq", "audit_log_id_seq")}
        for seq in saved:
            cur.execute("SELECT setval(%s, %s)", (seq, BIG))
    conn.autocommit = False
    try:
        rows = [
            {
                "site_id": 1, "category_id": 2, "activity_data": {"Meter": "M1"}, "total_emission": 1.5,
                "date_of_reporting": "2025-04-30", "created_by": 1, "upload_batch_id": "ci-bigint",
            },
            {
                "site_id": 1, "category_id": 3, "activity_data": {"Meter": "M1"}, "total_emission": 0.25,
                "date_of_reporting": "2025-04-30", "created_by": 1, "upload_batch_id": "ci-bigint",
            },
        ]
        source, fera = db.bulk_insert_emissions_with_conn(conn, rows, returning=True)
        assert isinstance(source, int) and source > BIG
        assert isinstance(fera, int) and fera > source

        db.link_fera_rows_with_conn(conn, [(source, fera)])
        db.audit_imported_rows_with_conn(conn, [(source, 1.5), (fera, 0.25)], 1, "ci bigint import")

        with conn.cursor() as cur:
            cur.execute("SELECT fera_linked_id FROM emission WHERE pk_id = %s", (source,))
            assert cur.fetchone()[0] == fera
            cur.execute(
                "SELECT id, entity_id FROM audit_log WHERE entity_type = 'emission' AND entity_id = ANY(%s) ORDER BY entity_id",
                ([source, fera],),
            )
            audits = cur.fetchall()
            assert [entity for _, entity in audits] == [source, fera]
            assert all(isinstance(i, int) and i > BIG for i, _ in audits)
    finally:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            for seq, (last_value, is_called) in saved.items():
                cur.execute("SELECT setval(%s, %s, %s)", (seq, last_value, is_called))
