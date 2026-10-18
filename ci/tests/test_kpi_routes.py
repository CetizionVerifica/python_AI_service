"""KPI-12 · /v1/kpi/* routes: ESG-lite's service key, company scoping of the
document, the upload the drop zone uses, and "AI not available right now".

reference_data.sql: company 1 owns sites 1 and 2; user 1 (User, site 1),
user 2 (Manager, sites 1 and 2 via user_sites). Added here: company 70 with
site 70 and its user 71, a Superadmin 73, and uploaded_documents rows that
point at the ci/fixtures/kpi12 files (the local-path storage a dev box uses).
The model is a fake; nothing leaves the machine.
"""
import json
import os
import pathlib
import time

import jwt
import pytest

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "kpi12"
SERVICE = {"X-Service-Key": os.environ.get("AI_SERVICE_KEY", "")}

USER_A, MANAGER_A, USER_B, SUPERADMIN = 1, 2, 71, 73
DOC_A = 7101  # water bill uploaded by user 1 (company 1)
DOC_A_MANAGER = 7102  # HR summary uploaded by the Manager (company 1 through user_sites)
DOC_B = 7103  # waste manifest uploaded by user 71 (company 70)
DOC_NO_OWNER = 7104  # from before uploaded_by existed
DOC_ROOT = 7105  # uploaded by the Superadmin: no company
DOC_IMPORTED = 7106  # file consumed
DOC_GONE = 7107  # local file missing
DOC_EXE = 7108  # unsupported type

WATER_KPIS = [
    {
        "code": "WATER_WITHDRAWAL",
        "question": "How much water did the site withdraw?",
        "unit": "m3",
        "allowed_units": ["m3", "kL"],
        "breakdowns": {"by": ["source"], "values": ["groundwater", "surface", "third_party"]},
    }
]


def token(user_id, role="User"):
    payload = {"userId": user_id, "role": role, "siteId": None, "exp": int(time.time()) + 3600}
    return {"Authorization": "Bearer " + jwt.encode(payload, os.environ["AUTH_JWT_SECRET"], algorithm="HS256")}


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def __call__(self, system, user, max_tokens):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer)


@pytest.fixture(scope="module")
def world(throwaway_db):
    from app.core.database import ensure_uploaded_documents_table

    ensure_uploaded_documents_table()
    path = lambda name: str(FIXTURES / name)
    with throwaway_db.cursor() as cur:
        cur.execute("INSERT INTO company (company_id, name, address, contact_person) VALUES (70, 'Other Co', 'x', 'x')")
        cur.execute("INSERT INTO site (site_id, name, address, contact_person, company_id) VALUES (70, 'Other plant', 'x', 'x', 70)")
        cur.execute(
            """
            INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
              (71, 'B User', 'kpi-b-user@example.invalid', 'x', 'User', 70),
              (73, 'Root', 'kpi-root@example.invalid', 'x', 'Superadmin', NULL)
            """
        )
        rows = [
            (DOC_A, "water-bill.pdf", path("water-bill.pdf"), USER_A, "uploaded"),
            (DOC_A_MANAGER, "hr-summary.xlsx", path("hr-summary.xlsx"), MANAGER_A, "uploaded"),
            (DOC_B, "waste-manifest.pdf", path("waste-manifest.pdf"), USER_B, "uploaded"),
            (DOC_NO_OWNER, "water-bill.pdf", path("water-bill.pdf"), None, "uploaded"),
            (DOC_ROOT, "water-bill.pdf", path("water-bill.pdf"), SUPERADMIN, "uploaded"),
            (DOC_IMPORTED, "water-bill.pdf", path("water-bill.pdf"), USER_A, "imported"),
            (DOC_GONE, "water-bill.pdf", path("no-such-file.pdf"), USER_A, "uploaded"),
            (DOC_EXE, "tool.exe", path("water-bill.pdf"), USER_A, "uploaded"),
        ]
        for row in rows:
            cur.execute(
                "INSERT INTO uploaded_documents (id, document_name, cloudinary_url, uploaded_by, status) VALUES (%s, %s, %s, %s, %s)",
                row,
            )
    yield
    with throwaway_db.cursor() as cur:
        cur.execute("DELETE FROM uploaded_documents WHERE id BETWEEN 7101 AND 7199")
        cur.execute('DELETE FROM "user" WHERE user_id IN (71, 73)')
        cur.execute("DELETE FROM site WHERE site_id = 70")
        cur.execute("DELETE FROM company WHERE company_id = 70")


@pytest.fixture(scope="module")
def client(world):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def model(monkeypatch):
    """The water bill's replayed answer; tests may swap ``fake.answer``."""
    from app.kpi import model as kpi_model

    fake = FakeModel(json.loads((FIXTURES / "water-bill.model.json").read_text()))
    monkeypatch.setattr(kpi_model, "configured_ask", lambda: fake)
    return fake


def values_body(**extra):
    return {"company_id": 1, "document_id": DOC_A, "kpis": WATER_KPIS, **extra}


MAPPING_BODY = {
    "name": "Borewell water withdrawn",
    "question": "How much groundwater did the site withdraw?",
    "unit": "m3",
    "topic": "water",
    "catalogue": [
        {"framework": "GRI", "code": "303-3", "title": "Water withdrawal", "section": "GRI 303"},
        {"framework": "GRI", "code": "305-1", "title": "Direct (Scope 1) GHG emissions", "section": "GRI 305"},
    ],
}


# ---------------------------------------------------------------------------
# The service key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,body", [("/v1/kpi/suggest-values", values_body()), ("/v1/kpi/suggest-mapping", MAPPING_BODY)])
def test_suggest_routes_are_esg_lites_service_key_only(client, model, path, body):
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers={"X-Service-Key": "wrong"}, json=body).status_code == 401
    assert client.post(path, headers={"X-Service-Key": ""}, json=body).status_code == 401
    for user in (token(USER_A), token(MANAGER_A, "Manager"), token(SUPERADMIN, "Superadmin")):
        assert client.post(path, headers=user, json=body).status_code == 403, "a user's token is not enough"
    assert client.post(path, headers=SERVICE, json={}).status_code == 422
    assert client.post(path, headers=SERVICE, json=body).status_code == 200
    assert model.calls == 1, "the model is only reached with the service key"


def test_suggest_values_answer_shape(client, model):
    r = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(period_hint={"period_start": "2026-04-01", "period_end": "2026-04-30", "frequency": "monthly"}))
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data) == {"document_id", "document_name", "pages", "candidates"}
    assert data["document_id"] == DOC_A and data["document_name"] == "water-bill.pdf" and data["pages"] == 2
    assert {c["kpi_code"] for c in data["candidates"]} == {"WATER_WITHDRAWAL"}
    total = next(c for c in data["candidates"] if c["breakdown_key"] is None)
    assert total == {
        "kpi_code": "WATER_WITHDRAWAL",
        "value": 1200.0,
        "unit": "m3",
        "breakdown_key": None,
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
        "site_hint": "CI Plant A, Plot 1",
        "source_page": 2,
        "quote": "Water consumed: 1,200 m3",
        "confidence": 0.92,
        "why": "found 'Water consumed: 1,200 m3' on page 2",
    }


def test_suggestions_outside_the_request_are_dropped_end_to_end(client, model):
    r = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body())
    codes = {c["kpi_code"] for c in r.json()["candidates"]}
    assert codes == {"WATER_WITHDRAWAL"}, "ENERGY_TOTAL and WATER_DISCHARGE were answered but not asked for"


def test_mapping_answer_shape_and_catalogue_only(client, monkeypatch):
    from app.kpi import model as kpi_model

    fake = FakeModel({"suggestions": [{"framework": "GRI", "code": "303-3", "why": "withdrawal", "confidence": 0.9}, {"framework": "GRI", "code": "303-9", "why": "made up", "confidence": 0.9}]})
    monkeypatch.setattr(kpi_model, "configured_ask", lambda: fake)
    r = client.post("/v1/kpi/suggest-mapping", headers=SERVICE, json=MAPPING_BODY)
    assert r.status_code == 200, r.text
    assert r.json() == {"suggestions": [{"framework": "GRI", "code": "303-3", "title": "Water withdrawal", "section": "GRI 303", "why": "withdrawal", "confidence": 0.9}]}


# ---------------------------------------------------------------------------
# Company scoping of the document
# ---------------------------------------------------------------------------

def test_the_document_must_belong_to_the_company(client, model):
    ok = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body())
    assert ok.status_code == 200
    # Company 70 asking for company 1's water bill, and company 1 for company 70's manifest.
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(company_id=70)).status_code == 404
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=DOC_B)).status_code == 404
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(company_id=70, document_id=DOC_B)).status_code == 200
    # A company that doesn't exist, a document that doesn't exist.
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(company_id=999)).status_code == 404
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=999999)).status_code == 404
    assert model.calls == 2, "the model never sees a document outside the company"


def test_the_company_comes_from_the_uploaders_sites(client, model):
    # The Manager has no site of their own, only user_sites rows: still company 1.
    r = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=DOC_A_MANAGER))
    assert r.status_code == 200, r.text
    assert r.json()["document_name"] == "hr-summary.xlsx"


@pytest.mark.parametrize("doc", [DOC_NO_OWNER, DOC_ROOT, DOC_IMPORTED])
def test_documents_without_a_company_or_already_consumed_are_not_found(client, model, doc):
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=doc)).status_code == 404
    assert model.calls == 0


def test_when_esg_lite_names_the_user_the_document_must_be_theirs(client, model):
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(user_id=USER_A)).status_code == 200
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(user_id=MANAGER_A)).status_code == 404


def test_unreadable_documents(client, model):
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=DOC_GONE)).status_code == 410
    assert client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=DOC_EXE)).status_code == 422
    assert model.calls == 0


# ---------------------------------------------------------------------------
# AI not available
# ---------------------------------------------------------------------------

def test_no_model_or_a_failed_call_is_503_ai_not_available(client, monkeypatch):
    from app.kpi import model as kpi_model

    monkeypatch.setattr(kpi_model, "configured_ask", lambda: None)
    r = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body())
    assert r.status_code == 503 and r.json() == {"detail": "AI not available right now"}
    monkeypatch.setattr(kpi_model, "configured_ask", lambda: FakeModel(TimeoutError("slow")))
    r = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body())
    assert r.status_code == 503
    # Mapping still answers from the word match, below 0.6.
    r = client.post("/v1/kpi/suggest-mapping", headers=SERVICE, json=MAPPING_BODY)
    assert r.status_code == 200 and r.json()["suggestions"][0]["code"] == "303-3"
    assert r.json()["suggestions"][0]["confidence"] <= 0.5


# ---------------------------------------------------------------------------
# The upload behind "Fill from a document"
# ---------------------------------------------------------------------------

def test_upload_stores_the_document_for_the_signed_in_user(client, model, throwaway_db):
    pdf = (FIXTURES / "water-bill.pdf").read_bytes()
    assert client.post("/v1/kpi/documents", files={"file": ("water-bill.pdf", pdf, "application/pdf")}).status_code == 401
    assert client.post("/v1/kpi/documents", headers=SERVICE, files={"file": ("water-bill.pdf", pdf, "application/pdf")}).status_code == 403
    assert client.post("/v1/kpi/documents", headers=token(SUPERADMIN, "Superadmin"), files={"file": ("water-bill.pdf", pdf, "application/pdf")}).status_code == 403
    assert client.post("/v1/kpi/documents", headers=token(USER_A), files={"file": ("tool.exe", b"MZ", "application/octet-stream")}).status_code == 400
    assert client.post("/v1/kpi/documents", headers=token(USER_A), files={"file": ("empty.pdf", b"", "application/pdf")}).status_code == 400

    r = client.post("/v1/kpi/documents", headers=token(USER_A), files={"file": ("water-bill.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"document_id", "document_name"} and body["document_name"] == "water-bill.pdf"
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT uploaded_by, cloudinary_url FROM uploaded_documents WHERE id = %s", (body["document_id"],))
        uploaded_by, location = cur.fetchone()
    assert uploaded_by == USER_A
    try:
        ok = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=body["document_id"]))
        assert ok.status_code == 200 and ok.json()["candidates"]
        other = client.post("/v1/kpi/suggest-values", headers=SERVICE, json=values_body(document_id=body["document_id"], company_id=70))
        assert other.status_code == 404
    finally:
        pathlib.Path(location).unlink(missing_ok=True)
        with throwaway_db.cursor() as cur:
            cur.execute("DELETE FROM uploaded_documents WHERE id = %s", (body["document_id"],))
