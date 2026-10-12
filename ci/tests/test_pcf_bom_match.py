"""E2 2/3: the BOM reader and the material → factor matcher.

Sheets are built in memory, PDFs are stood in for by a text stub, and the LLM
is a stub (CI never calls it). The endpoint tests run against the throwaway
DB for the sign-in checks and the evidence row.
"""
import io
import json
import os
import time

import jwt
import openpyxl
import pytest

from app.services import pcf_bom, pcf_match
from app.services.pcf_sheet import SheetError


def xlsx(*sheets):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, rows in sheets:
        ws = book.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def no_llm(_messages):
    raise AssertionError("the LLM must not be called")


BOM = [
    ["Midal pilot BOM (placeholder numbers)"],
    [],
    ["Item code", "Material", "Qty", "UoM", "Supplier", "Country of origin", "Recycled %", "Type"],
    ["AL-01", "EC-grade aluminium ingot", 1020, "g", "Alba", "Bahrain", 20, "Raw material"],
    ["PK-01", "Wooden reel", 0.002, "pcs", "", "", "", "Packaging"],
    ["WS-01", "Aluminium scrap to recycler", 15, "g", "", "", "", "Waste"],
    ["OL-01", "Rolling oil", 2, "litre", "", "", "", ""],
]


# ---------------------------------------------------------------------- bom ---

def test_reads_a_bom_sheet_into_pcf_input_lines():
    r = pcf_bom.read_bom(xlsx(("BOM", BOM)), "bom.xlsx", call_llm=no_llm)
    assert r["header_line"] == 3
    assert r["mapping"]["name"]["header"] == "Material"
    assert r["mapping"]["quantity"]["header"] == "Qty"
    assert r["mapping"]["unit"]["header"] == "UoM"
    assert r["mapping"]["stage"]["header"] == "Type"
    lines = {l["values"]["name"].split(" (")[0]: l for l in r["lines"]}

    alu = lines["EC-grade aluminium ingot"]
    assert alu["values"]["name"] == "EC-grade aluminium ingot (AL-01)"
    assert alu["values"]["stage"] == "A1"
    assert alu["values"]["unit"] == "kg"
    assert alu["values"]["quantity"] == pytest.approx(1.02)
    assert alu["values"]["supplier_name"] == "Alba"
    assert alu["values"]["origin_country"] == "Bahrain"
    assert alu["values"]["recycled_share_pct"] == 20
    assert alu["values"]["data_type"] == "secondary"
    assert alu["confidence"] >= 80 and alu["problems"] == []
    assert "converted to kg" in alu["reason"]

    assert lines["Wooden reel"]["values"]["stage"] == "A3_packaging"
    assert lines["Wooden reel"]["values"]["unit"] == "unit"
    assert lines["Aluminium scrap to recycler"]["values"]["stage"] == "A3_waste"

    oil = lines["Rolling oil"]
    assert oil["values"]["unit"] == "litre"
    assert oil["problems"] and "litre" in oil["problems"][0]
    assert oil["confidence"] <= 30
    assert r["lines_with_problems"] == 1
    assert r["ai_used"] is False


def test_batch_size_and_mass_check():
    sheet = [["Material", "Quantity (kg)"], ["Aluminium", 1000], ["Copper", 2]]
    r = pcf_bom.read_bom(xlsx(("A", sheet)), "a.xlsx", batch_units=1000, mass_per_unit_kg=1, call_llm=no_llm)
    assert r["batch_units"] == 1000
    assert r["lines"][0]["values"]["unit"] == "kg"
    assert r["lines"][0]["values"]["quantity"] == pytest.approx(1)
    assert "÷1000" in r["lines"][0]["reason"]
    assert r["warnings"] == []  # 1.002 kg against 1 kg is fine

    r = pcf_bom.read_bom(xlsx(("A", sheet)), "a.xlsx", batch_units=1, mass_per_unit_kg=1, call_llm=no_llm)
    assert any("add up to 1,002 kg" in w for w in r["warnings"])


def test_no_unit_anywhere_is_a_problem_not_a_guess():
    r = pcf_bom.read_bom(b"Material,Qty\nAluminium,1\n", "a.csv", call_llm=no_llm)
    assert r["lines"][0]["problems"] == ["No unit; the quantity column header names none either"]
    assert any("every line needs a unit" in w for w in r["warnings"])
    with pytest.raises(SheetError):
        pcf_bom.read_bom(xlsx(("A", BOM)), "a.xlsx", batch_units=0, call_llm=no_llm)
    with pytest.raises(SheetError):
        pcf_bom.read_bom(b"x", "a.xls", call_llm=no_llm)


def test_stage_words_and_packaging_names():
    assert pcf_bom.stage_from("Packaging", "Film") == ("A3_packaging", "packaging")
    assert pcf_bom.stage_from("", "Corrugated carton") == ("A3_packaging", "carton")
    assert pcf_bom.stage_from("Inbound freight", "Truck") == ("A2", "freight")
    assert pcf_bom.stage_from("", "Primary aluminium") == ("A1", None)


def test_pdf_goes_through_text_and_the_ai():
    calls = []

    def llm(messages):
        calls.append(messages)
        return json.dumps(
            {
                "batch": {"quantity": 1000, "unit": "kg"},
                "lines": [
                    {"material": "Aluminium rod", "quantity": 1020, "unit": "kg", "supplier": "Alba", "origin": "BH", "confidence": 95, "reason": "Line 1 of the BOM table"},
                    {"material": "Pallet", "quantity": 1, "unit": "pcs", "confidence": 60, "reason": "Packaging row"},
                    {"material": "Glue", "quantity": 0.5, "unit": "tons", "confidence": 70, "reason": "Row 3"},
                ],
            }
        )

    r = pcf_bom.read_bom(b"%PDF-1.4 fake", "bom.pdf", batch_units=1000, call_llm=llm, extract_text=lambda c, n: "Bill of materials\nAluminium rod 1020 kg ...")
    assert r["ai_used"] is True and r["headers"] == []
    assert [l["by"] for l in r["lines"]] == ["ai", "ai", "ai"]
    assert r["lines"][0]["values"] == {
        "name": "Aluminium rod", "stage": "A1", "unit": "kg", "quantity": pytest.approx(1.02), "supplier_name": "Alba",
        "origin_country": "BH", "recycled_share_pct": 0, "data_type": "secondary",
    }
    assert r["lines"][0]["confidence"] == 85  # capped: read from text
    assert r["lines"][1]["values"]["stage"] == "A3_packaging"
    assert r["lines"][2]["problems"] and "ton" in r["lines"][2]["problems"][0]
    assert r["warnings"] == []  # batch given, so no reminder
    assert "Return ONLY a JSON object" in calls[0][0]["content"]

    r = pcf_bom.read_bom(b"%PDF", "bom.pdf", call_llm=llm, extract_text=lambda c, n: "x" * 50)
    assert any("per 1000 kg" in w for w in r["warnings"])
    with pytest.raises(SheetError):
        pcf_bom.read_bom(b"%PDF", "bom.pdf", call_llm=llm, extract_text=lambda c, n: "")
    with pytest.raises(SheetError):
        pcf_bom.read_bom(b"%PDF", "bom.pdf", call_llm=no_llm)


def test_ai_places_the_quantity_column_only_when_headers_cannot():
    sheet = [["Material", "Per 1 kg rod", "UoM"], ["Aluminium", 1.02, "kg"]]

    def llm(messages):
        assert "quantity" in messages[0]["content"]
        return json.dumps({"mapping": {"quantity": {"header": "Per 1 kg rod", "confidence": 99, "reason": "The only numeric column"}}})

    r = pcf_bom.read_bom(xlsx(("A", sheet)), "a.xlsx", call_llm=llm)
    assert r["ai_used"] is True
    assert r["mapping"]["quantity"] == {"header": "Per 1 kg rod", "confidence": 90, "reason": "The only numeric column", "by": "ai"}
    assert r["lines"][0]["confidence"] == 90


# -------------------------------------------------------------------- match ---

FACTORS = [
    {"material_factor_id": 1, "name": "Aluminium, primary ingot", "material_group": "aluminium", "unit": "kg", "geography": "Middle East", "source": "IAI", "source_year": 2023, "licence": "open", "recycled_variant": False},
    {"material_factor_id": 2, "name": "Aluminium, primary ingot", "material_group": "aluminium", "unit": "kg", "geography": "Europe", "source": "IAI", "source_year": 2023, "licence": "open", "recycled_variant": False},
    {"material_factor_id": 3, "name": "Aluminium, recycled (MiRecAL)", "material_group": "aluminium", "unit": "kg", "geography": "Middle East", "source": "Midal", "source_year": 2024, "licence": "supplier", "recycled_variant": True},
    {"material_factor_id": 4, "name": "Copper cathode", "material_group": "copper", "unit": "kg", "geography": "Global", "source": "ICA", "source_year": 2022, "licence": "open", "recycled_variant": False},
    {"material_factor_id": 5, "name": "Wooden reel", "material_group": "packaging", "unit": "unit", "geography": "Global", "source": "ecoinvent", "source_year": 2023, "licence": "ecoinvent", "recycled_variant": False},
]


def test_matches_by_name_group_geography_and_variant():
    r = pcf_match.match_factors(
        [
            {"id": "a", "name": "EC-grade aluminium ingot", "unit": "kg", "origin_country": "Bahrain"},
            {"id": "b", "name": "Aluminium scrap, recycled", "unit": "kg", "recycled_share_pct": 100},
            {"id": "c", "name": "Reel, wooden", "unit": "pcs"},
            {"id": "d", "name": "Rolling oil", "unit": "litre"},
        ],
        FACTORS,
        geography_hint="GCC",
        call_llm=no_llm,
    )
    assert r["ai_used"] is False
    a, b, c, d = r["lines"]
    assert [s["material_factor_id"] for s in a["suggestions"]] == [1, 2, 3]
    assert a["suggestions"][0]["confidence"] >= 60 and not a["unmatched"]
    assert "Middle East covers" in a["suggestions"][0]["reason"]
    assert "IAI 2023" in a["suggestions"][0]["reason"]
    assert a["suggestions"][0]["confidence"] > a["suggestions"][1]["confidence"]  # region beats Europe
    assert b["suggestions"][0]["material_factor_id"] == 3
    assert c["suggestions"][0]["material_factor_id"] == 5 and not c["unmatched"]
    assert d["unmatched"] is True
    assert all(s["confidence"] <= 90 for line in r["lines"] for s in line["suggestions"])
    assert r["unmatched_lines"] == 1


def test_ai_picks_among_candidates_when_names_are_weak():
    calls = []

    def llm(messages):
        calls.append(json.loads(messages[1]["content"]))
        return json.dumps({"material_factor_id": 4, "confidence": 95, "reason": "Cu cathode is copper cathode"})

    r = pcf_match.match_factors([{"id": 1, "name": "Cu-CATH-A 99.99", "unit": "kg"}], FACTORS, call_llm=llm)
    s = r["lines"][0]["suggestions"][0]
    assert s == {"material_factor_id": 4, "name": "Copper cathode", "confidence": 85, "reason": "Cu cathode is copper cathode", "by": "ai"}
    assert r["ai_used"] is True and not r["lines"][0]["unmatched"]
    assert {c["material_factor_id"] for c in calls[0]["candidates"]} <= {f["material_factor_id"] for f in FACTORS}

    # A pick outside the candidates, or a broken answer, keeps the rule ranking.
    r = pcf_match.match_factors([{"id": 1, "name": "Cu-CATH-A 99.99", "unit": "kg"}], FACTORS, call_llm=lambda m: '{"material_factor_id": 99}')
    assert r["ai_used"] is False
    r = pcf_match.match_factors([{"id": 1, "name": "Cu-CATH-A 99.99", "unit": "kg"}], FACTORS, call_llm=lambda m: "not json")
    assert r["ai_used"] is False


# ----------------------------------------------------------------- endpoint ---

PCF_SUPERADMIN, PCF_ADMIN = 63, 64
USER_A, MANAGER_A = 1, 2  # reference_data.sql


def bearer(user_id):
    payload = {"userId": user_id, "role": "x", "siteId": None, "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(payload, os.environ["AUTH_JWT_SECRET"], algorithm="HS256")}


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def client(throwaway_db, monkeypatch_module):
    from fastapi.testclient import TestClient

    from app.api import pcf as pcf_api
    from app.core.database import ensure_pcf_evidence_table
    from app.main import app

    monkeypatch_module.setattr(pcf_api, "_llm", no_llm)
    monkeypatch_module.setattr(pcf_api, "_extract_text", lambda content, name: "Bill of materials " + content.decode("latin-1"))
    ensure_pcf_evidence_table()
    with throwaway_db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
              (63, 'PCF Root 2', 'pcf-root2@example.invalid', 'x', 'Superadmin', NULL),
              (64, 'PCF Admin 2', 'pcf-admin2@example.invalid', 'x', 'Admin', 1)
            """
        )
    yield TestClient(app)
    with throwaway_db.cursor() as cur:
        cur.execute('DELETE FROM "user" WHERE user_id IN (63, 64)')
        cur.execute("DELETE FROM pcf_evidence")


BOM_URL = "/v1/pcf/parse-bom"
MATCH_URL = "/v1/pcf/match-factors"


def upload(content=None, name="bom.xlsx", **form):
    return {"files": {"file": (name, content if content is not None else xlsx(("BOM", BOM)))}, "data": form}


def test_parse_bom_needs_a_manager_or_superadmin(client):
    assert client.post(BOM_URL, **upload()).status_code == 401
    assert client.post(BOM_URL, headers=bearer(USER_A), **upload()).status_code == 403
    assert client.post(BOM_URL, headers=bearer(PCF_ADMIN), **upload()).status_code == 403
    assert client.post(MATCH_URL, headers=bearer(USER_A), json={"lines": [{"name": "x"}], "factors": FACTORS}).status_code == 403
    for who in (MANAGER_A, PCF_SUPERADMIN):
        r = client.post(BOM_URL, headers=bearer(who), **upload(keep_evidence="false"))
        assert r.status_code == 200, r.text
        assert r.json()["total_lines"] == 4 and r.json()["evidence"] is None


def test_parse_bom_keeps_the_file_as_evidence(client, throwaway_db):
    r = client.post(BOM_URL, headers=bearer(MANAGER_A), **upload(study_id="7", batch_units="1000"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_units"] == 1000
    ev = body["evidence"]
    assert ev["document_name"] == "bom.xlsx" and ev["stored"] == "local"
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT kind, document_name, pcf_study_id, uploaded_by, file_size FROM pcf_evidence WHERE id = %s", (ev["id"],))
        assert cur.fetchone() == ("bom", "bom.xlsx", 7, MANAGER_A, len(xlsx(("BOM", BOM))))
    assert os.path.exists(ev["url"])
    os.remove(ev["url"])


def test_parse_bom_refuses_bad_input(client, monkeypatch):
    from app.api import pcf as pcf_api

    monkeypatch.setattr(pcf_api, "_llm", lambda m: "not json at all")
    h = bearer(MANAGER_A)
    assert client.post(BOM_URL, headers=h, **upload(mapping="{")).status_code == 422
    assert client.post(BOM_URL, headers=h, **upload(batch_units="0")).status_code == 422
    assert client.post(BOM_URL, headers=h, **upload(name="bom.docx")).status_code == 400
    assert client.post(BOM_URL, headers=h, **upload(content=b"", name="bom.csv")).status_code == 400
    r = client.post(BOM_URL, headers=h, **upload(content=b"%PDF-1.4", name="bom.pdf", keep_evidence="false"))
    assert r.status_code == 422  # the AI answered nonsense
    assert "bill of materials" in r.json()["detail"]


def test_match_factors_endpoint(client):
    r = client.post(
        MATCH_URL,
        headers=bearer(MANAGER_A),
        json={"lines": [{"id": "l1", "name": "EC-grade aluminium ingot", "unit": "kg"}], "factors": FACTORS, "geography_hint": "Bahrain"},
    )
    assert r.status_code == 200, r.text
    line = r.json()["lines"][0]
    assert line["id"] == "l1" and line["suggestions"][0]["material_factor_id"] == 1
    assert client.post(MATCH_URL, headers=bearer(MANAGER_A), json={"lines": [], "factors": FACTORS}).status_code == 422
