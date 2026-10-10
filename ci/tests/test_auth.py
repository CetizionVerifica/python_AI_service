"""Every /v1 route needs a caller, and a user only reaches their own sites.

Browser calls carry the ESG-lite sign-in token (HS256, AUTH_JWT_SECRET);
ESG-lite's server-to-server calls carry X-Service-Key, accepted only on
/v1/column-config/* and /v1/sea-route. Users are scoped to their sites the
way ESG-lite's accessibleSiteIds scopes them.
"""
import os
import time

import jwt
import pytest

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

# reference_data.sql: company 1 owns sites 1 and 2; user 1 (User, site 1),
# user 2 (Manager, sites 1 and 2). Added here: company 50 with site 50.
USER_A = 1  # User at site 1 (company 1)
MANAGER_A = 2  # Manager at sites 1, 2
ADMIN_A = 51  # Admin of company 1
USER_B = 52  # User at site 50 (company 50)
SUPERADMIN = 53
SITE_A, SITE_A2, SITE_B = 1, 2, 50


def token(user_id, role="User", secret=None, exp_in=3600, **extra):
    payload = {"userId": user_id, "role": role, "siteId": None, "exp": int(time.time()) + exp_in, **extra}
    return jwt.encode(payload, secret or os.environ["AUTH_JWT_SECRET"], algorithm="HS256")


def bearer(user_id, **kw):
    return {"Authorization": f"Bearer {token(user_id, **kw)}"}


SERVICE = {"X-Service-Key": os.environ.get("AI_SERVICE_KEY", "")}


@pytest.fixture(scope="module")
def world(throwaway_db):
    from app.core.database import ensure_uploaded_documents_table

    ensure_uploaded_documents_table()
    with throwaway_db.cursor() as cur:
        cur.execute(INVOICE_TABLE)
        cur.execute(
            "INSERT INTO company (company_id, name, address, contact_person) VALUES (50, 'Other Co', 'x', 'x')"
        )
        cur.execute(
            "INSERT INTO site (site_id, name, address, contact_person, company_id) VALUES (50, 'Other plant', 'x', 'x', 50)"
        )
        cur.execute(
            """
            INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
              (51, 'A Admin', 'a-admin@example.invalid', 'x', 'Admin', 1),
              (52, 'B User', 'b-user@example.invalid', 'x', 'User', 50),
              (53, 'Root', 'root@example.invalid', 'x', 'Superadmin', NULL)
            """
        )
        cur.execute(
            """
            INSERT INTO invoice (file_name, cloudinary_url, cloudinary_public_id, uploaded_by, site_id) VALUES
              ('a1.pdf', 'u', 'p', 1, 1),
              ('a2.pdf', 'u', 'p', 2, 2),
              ('b.pdf', 'u', 'p', 52, 50),
              ('a-nosite.pdf', 'u', 'p', 1, NULL),
              ('b-nosite.pdf', 'u', 'p', 52, NULL)
            RETURNING invoice_id, file_name
            """
        )
        ids = {name: invoice_id for invoice_id, name in cur.fetchall()}
    yield ids
    with throwaway_db.cursor() as cur:
        cur.execute("DELETE FROM invoice WHERE invoice_id > 0")
        cur.execute("DELETE FROM uploaded_documents WHERE id > 0")
        cur.execute('DELETE FROM "user" WHERE user_id IN (51, 52, 53)')
        cur.execute("DELETE FROM site WHERE site_id = 50")
        cur.execute("DELETE FROM company WHERE company_id = 50")


@pytest.fixture(scope="module")
def client(world):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


ROUTER_ENDPOINTS = [
    ("get", "/v1/invoices"),
    ("get", "/v1/invoices/1"),
    ("get", "/v1/invoices/1/serve"),
    ("delete", "/v1/invoices/1"),
    ("post", "/v1/invoices/upload"),
    ("post", "/v1/extract"),
    ("get", "/v1/emission-categories"),
    ("get", "/v1/emission-factors/uploads"),
    ("post", "/v1/emission-factors/parse-excel"),
    ("post", "/v1/category-mappings/parse-excel"),
    ("post", "/v1/column-config/infer-columns"),
    ("post", "/v1/column-config/infer-all-columns"),
    ("post", "/v1/excel/upload"),
    ("post", "/v1/excel/import"),
    ("post", "/v1/excel/preview"),
    ("post", "/v1/excel/unique-categories"),
    ("post", "/v1/sea-route"),
]


def test_health_and_root_stay_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("method,path", ROUTER_ENDPOINTS)
def test_no_token_is_401(client, method, path):
    assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {token(USER_A, secret='some-other-secret')}"},
        {"Authorization": f"Bearer {token(USER_A, exp_in=-60)}"},
        {"Authorization": f"Bearer {token(9999)}"},  # no such user
        {"Authorization": f"Basic {token(USER_A)}"},
        {"Authorization": f"Bearer {jwt.encode({'userId': USER_A}, os.environ['AUTH_JWT_SECRET'], algorithm='HS256')}"},  # no exp
        {"X-Service-Key": "wrong-key"},
    ],
)
def test_bad_credentials_are_401(client, headers):
    assert client.get("/v1/invoices", headers=headers).status_code == 401
    assert client.post("/v1/sea-route", headers=headers, json={}).status_code == 401


def test_service_key_only_on_column_config_and_sea_route(client):
    # Past auth: the empty body fails validation instead.
    assert client.post("/v1/sea-route", headers=SERVICE, json={}).status_code == 422
    assert client.post("/v1/column-config/infer-columns", headers=SERVICE, json={}).status_code == 422
    assert client.post("/v1/column-config/infer-all-columns", headers=SERVICE, json={}).status_code == 422
    for method, path in ROUTER_ENDPOINTS:
        if path.startswith(("/v1/sea-route", "/v1/column-config")):
            continue
        assert getattr(client, method)(path, headers=SERVICE).status_code == 403, path


def test_users_may_call_column_config_and_sea_route(client):
    assert client.post("/v1/sea-route", headers=bearer(USER_A), json={}).status_code == 422


def test_setup_screens_are_superadmin_only(client, monkeypatch):
    from app.services import category_matcher

    monkeypatch.setattr(category_matcher, "fetch_emission_categories", lambda: [])
    for user in (USER_A, MANAGER_A, ADMIN_A):
        assert client.get("/v1/emission-categories", headers=bearer(user)).status_code == 403
        assert client.get("/v1/emission-factors/uploads", headers=bearer(user)).status_code == 403
        assert client.post("/v1/category-mappings/parse-excel", headers=bearer(user)).status_code == 403
    assert client.get("/v1/emission-categories", headers=bearer(SUPERADMIN)).status_code == 200
    assert client.get("/v1/emission-factors/uploads", headers=bearer(SUPERADMIN)).status_code == 200


def _names(client, user, **params):
    res = client.get("/v1/invoices", headers=bearer(user), params=params)
    assert res.status_code == 200, res.text
    return sorted(inv["file_name"] for inv in res.json())


def test_invoice_list_is_scoped_to_the_callers_sites(client, world):
    assert _names(client, USER_A) == ["a-nosite.pdf", "a1.pdf"]
    assert _names(client, MANAGER_A) == ["a1.pdf", "a2.pdf"]
    assert _names(client, ADMIN_A) == ["a1.pdf", "a2.pdf"]
    assert _names(client, USER_B) == ["b-nosite.pdf", "b.pdf"]
    assert _names(client, SUPERADMIN) == sorted(world)
    # A user_id filter cannot widen the scope.
    assert _names(client, USER_B, user_id=USER_A) == []
    # Asking for another company's site is refused.
    assert client.get("/v1/invoices", headers=bearer(USER_A), params={"site_id": SITE_B}).status_code == 403
    assert client.get("/v1/invoices", headers=bearer(USER_A), params={"site_id": SITE_A2}).status_code == 403
    assert _names(client, MANAGER_A, site_id=SITE_A2) == ["a2.pdf"]


def test_foreign_invoice_read_serve_delete_is_404(client, world, monkeypatch):
    from app.core import cloudinary_service

    monkeypatch.setattr(cloudinary_service, "delete_file", lambda public_id: None)
    foreign = world["b.pdf"]
    foreign_nosite = world["b-nosite.pdf"]
    for invoice_id in (foreign, foreign_nosite):
        assert client.get(f"/v1/invoices/{invoice_id}", headers=bearer(USER_A)).status_code == 404
        assert client.get(f"/v1/invoices/{invoice_id}/serve", headers=bearer(USER_A)).status_code == 404
        assert client.delete(f"/v1/invoices/{invoice_id}", headers=bearer(ADMIN_A)).status_code == 404
        assert client.post("/v1/extract", headers=bearer(USER_A), data={"invoice_id": str(invoice_id)}).status_code == 404
    res = client.request(
        "DELETE", "/v1/invoices/bulk", headers=bearer(ADMIN_A), json={"ids": [world["a1.pdf"], foreign]}
    )
    assert res.status_code == 404
    # Nothing was deleted, and the owners still see their invoices.
    assert client.get(f"/v1/invoices/{foreign}", headers=bearer(USER_B)).status_code == 200
    assert client.get(f"/v1/invoices/{foreign_nosite}", headers=bearer(USER_B)).status_code == 200
    assert client.get(f"/v1/invoices/{world['a1.pdf']}", headers=bearer(USER_A)).status_code == 200
    assert client.get(f"/v1/invoices/{foreign}", headers=bearer(SUPERADMIN)).status_code == 200


def test_invoice_upload_uses_the_token_user_and_checks_the_site(client, throwaway_db, monkeypatch, tmp_path):
    from app.api import invoices as invoices_api
    from app.schemas.invoice import ExtractionResponse

    async def save_upload(file):
        path = tmp_path / "bill.pdf"
        path.write_bytes(b"%PDF-1.4")
        return path

    async def process_document(path, filename, **kw):
        return ExtractionResponse(filename=filename)

    monkeypatch.setattr(invoices_api.storage, "save_upload", save_upload)
    monkeypatch.setattr(invoices_api.storage, "cleanup", lambda path: None)
    monkeypatch.setattr(invoices_api.pipeline, "process_document", process_document)
    monkeypatch.setattr(
        invoices_api.cloudinary_service,
        "upload_file",
        lambda path, folder=None: {"secure_url": "https://x/bill.pdf", "public_id": "invoices/bill", "bytes": 8},
    )
    files = {"file": ("bill.pdf", b"%PDF-1.4", "application/pdf")}

    res = client.post(
        "/v1/invoices/upload",
        headers=bearer(USER_A),
        files=files,
        data={"site_id": str(SITE_B), "uploaded_by": str(USER_B)},
    )
    assert res.status_code == 403

    res = client.post(
        "/v1/invoices/upload",
        headers=bearer(USER_A),
        files=files,
        data={"site_id": str(SITE_A), "uploaded_by": str(USER_B)},
    )
    assert res.status_code == 200, res.text
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT uploaded_by, site_id FROM invoice WHERE invoice_id = %s", (res.json()["invoice_id"],))
        assert cur.fetchone() == (USER_A, SITE_A)


def test_excel_wizard_checks_site_document_and_overrides_user(client, throwaway_db, monkeypatch):
    from app.api import excel as excel_api

    monkeypatch.setattr(excel_api.cloudinary_service, "is_configured", lambda: False)
    csv = ("rows.csv", b"Fuel,Qty\nDiesel,1\n", "text/csv")

    res = client.post("/v1/excel/upload", headers=bearer(USER_A), files={"file": csv})
    assert res.status_code == 200, res.text
    doc_a = res.json()["document_id"]
    res = client.post("/v1/excel/upload", headers=bearer(USER_B), files={"file": csv})
    doc_b = res.json()["document_id"]
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT id, uploaded_by FROM uploaded_documents WHERE id IN (%s, %s) ORDER BY id", (doc_a, doc_b))
        assert cur.fetchall() == [(doc_a, USER_A), (doc_b, USER_B)]

    calls = []

    def fake_import(**kw):
        calls.append(kw)
        return {"inserted": 0, "skipped": 0, "total_rows": 0}

    monkeypatch.setattr(excel_api, "import_all_rows", fake_import)
    body = {
        "document_id": doc_a, "mappings": {}, "selected_categories": [],
        "site_id": SITE_A, "category_id": 1, "date_of_reporting": "2025-01-01", "user_id": USER_B,
    }
    # Another company's site.
    assert client.post("/v1/excel/import", headers=bearer(USER_A), json={**body, "site_id": SITE_B}).status_code == 403
    assert client.post("/v1/excel/import", headers=bearer(USER_A), json={**body, "site_id": str(SITE_B)}).status_code == 403
    assert client.post("/v1/excel/preview", headers=bearer(USER_A), json={**body, "site_id": SITE_B}).status_code == 403
    # Someone else's document.
    assert client.post("/v1/excel/import", headers=bearer(USER_A), json={**body, "document_id": doc_b}).status_code == 404
    assert client.post("/v1/excel/unique-categories", headers=bearer(USER_A), json={"document_id": doc_b}).status_code == 404
    assert calls == []

    res = client.post("/v1/excel/import", headers=bearer(USER_A), json=body)
    assert res.status_code == 200, res.text
    assert calls[-1]["user_id"] == USER_A and calls[-1]["site_id"] == SITE_A

    # A Superadmin may use any document and site.
    res = client.post("/v1/excel/import", headers=bearer(SUPERADMIN), json={**body, "document_id": doc_b, "site_id": SITE_B})
    assert res.status_code == 200, res.text
    assert calls[-1]["user_id"] == SUPERADMIN
