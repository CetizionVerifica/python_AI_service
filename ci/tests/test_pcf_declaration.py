"""C06 1/5: the supplier declaration reader (PACT JSON without the AI, PDF
through a text stub and an LLM stub). The endpoint tests run against the
throwaway DB for the sign-in checks and the evidence row."""
import json
import os
import time

import jwt
import pytest

from app.services import pcf_declaration as decl


def no_llm(_messages):
    raise AssertionError("the LLM must not be called")


PACT_3 = {
    "id": "d9be4477-e351-45b3-acd9-e1da05e6f633",
    "specVersion": "3.0.0",
    "created": "2025-02-01T00:00:00Z",
    "status": "Active",
    "validityPeriodStart": "2025-01-01T00:00:00Z",
    "validityPeriodEnd": "2027-12-31T00:00:00Z",
    "companyName": "Alba Smelter (placeholder)",
    "companyIds": ["urn:pact:example.com:company-id:1"],
    "productDescription": "EC-grade aluminium ingot",
    "productIds": ["urn:pact:example.com:supplier-id:AL-01"],
    "productNameCompany": "EC-grade aluminium ingot 99.7",
    "pcf": {
        "declaredUnitOfMeasurement": "kilogram",
        "declaredUnitAmount": "1",
        "productMassPerDeclaredUnit": "1",
        "referencePeriodStart": "2024-01-01T00:00:00Z",
        "referencePeriodEnd": "2025-01-01T00:00:00Z",
        "boundaryProcessesDescription": "Cradle to gate: bauxite mining, refining, smelting, casting.",
        "pcfExcludingBiogenicUptake": "8.4",
        "pcfIncludingBiogenicUptake": "8.4",
        "fossilGhgEmissions": "8.4",
        "fossilCarbonContent": "0",
        "biogenicCarbonContent": "0",
        "ipccCharacterizationFactors": ["AR6"],
        "crossSectoralStandards": ["ISO Standard 14067", "PACT-3.0"],
        "exemptedEmissionsPercent": "0",
        "assurance": {"coverage": "product level", "providerName": "Verifier Ltd", "standardName": "ISO 14064-3"},
    },
}


def as_json(obj):
    return json.dumps(obj).encode()


def test_reads_a_pact_3_footprint_without_the_ai():
    r = decl.read_declaration(as_json(PACT_3), "ingot.json", "cradle_to_gate", "kg", call_llm=no_llm)
    f = r["fields"]
    assert r["kind"] == "pact" and r["ai_used"] is False
    assert f["value_kgco2e"]["value"] == 8.4 and f["value_kgco2e"]["by"] == "pact" and f["value_kgco2e"]["confidence"] == 100
    assert f["declared_unit"]["value"] == "kg"
    assert f["boundary"]["value"] == "cradle_to_gate"
    assert f["standard"]["value"] == "iso14067"
    assert f["gwp_set"]["value"] == "AR6"
    assert f["reference_start"]["value"] == "2024-01-01" and f["reference_end"]["value"] == "2025-01-01"
    assert f["valid_from"]["value"] == "2025-01-01" and f["valid_to"]["value"] == "2027-12-31"
    assert f["verifier"]["value"] == "Verifier Ltd"
    assert f["supplier_name"]["value"].startswith("Alba") and f["product_name"]["value"].startswith("EC-grade")
    assert r["boundary_mismatch"] is False and r["unit_mismatch"] is False
    assert r["problems"] == [] and r["low_confidence_fields"] == []
    assert all("reason" in v and v["reason"] for v in f.values() if v["value"] is not None)


def test_reads_a_pact_2_footprint_per_declared_unit_amount():
    pact2 = {
        "id": "x",
        "specVersion": "2.2.0",
        "companyName": "Reel Co",
        "productNameCompany": "Wooden reel 1.2 m",
        "validityPeriodEnd": "2026-06-30T00:00:00Z",
        "pcf": {
            "declaredUnit": "piece",
            "unitaryProductAmount": "10",
            "pCfExcludingBiogenic": "120",
            "referencePeriodStart": "2023-01-01T00:00:00Z",
            "referencePeriodEnd": "2023-12-31T00:00:00Z",
            "characterizationFactors": "AR5",
            "crossSectoralStandardsUsed": ["GHG Protocol Product standard"],
            "assurance": {"assurance": False, "coverage": "none"},
        },
    }
    r = decl.read_declaration(as_json(pact2), "reel.json", "cradle_to_gate", "unit", call_llm=no_llm)
    f = r["fields"]
    assert f["declared_unit"]["value"] == "unit"
    assert f["value_kgco2e"]["value"] == 12 and "÷ declaredUnitAmount 10" in f["value_kgco2e"]["reason"]
    assert f["gwp_set"]["value"] == "AR5" and f["standard"]["value"] == "ghg_protocol_product"
    assert f["boundary"]["value"] == "cradle_to_gate" and f["boundary"]["confidence"] == 70  # PACT default
    assert f["verifier"]["value"] is None and "not assured" in f["verifier"]["reason"]
    assert f["valid_to"]["value"] == "2026-06-30"


def test_pact_megajoule_and_tonne_conversions_and_mismatches():
    pact = json.loads(json.dumps(PACT_3))
    pact["pcf"]["declaredUnitOfMeasurement"] = "megajoule"
    pact["pcf"]["pcfExcludingBiogenicUptake"] = "0.1"
    r = decl.read_declaration(as_json(pact), "energy.json", "cradle_to_gate", "kWh", call_llm=no_llm)
    assert r["fields"]["declared_unit"]["value"] == "kWh"
    assert r["fields"]["value_kgco2e"]["value"] == pytest.approx(0.36)

    # Per tonne for an input per kg: converted, not a mismatch.
    pact = json.loads(json.dumps(PACT_3))
    pact["pcf"]["boundaryProcessesDescription"] = "Cradle to grave including use and end of life."
    pact["pcf"]["declaredUnitOfMeasurement"] = "liter"
    r = decl.read_declaration(as_json(pact), "oil.json", "cradle_to_gate", "kg", call_llm=no_llm)
    assert r["boundary_mismatch"] is True and r["fields"]["boundary"]["value"] == "cradle_to_grave"
    assert any("cradle-to-grave" in w and "can't be accepted" in w for w in r["warnings"])
    assert r["unit_mismatch"] is False  # unknown unit is a problem, not a mismatch
    assert r["problems"] and "liter" in r["problems"][0]


def test_tonne_figure_becomes_per_kg_for_a_kg_input():
    pact = json.loads(json.dumps(PACT_3))
    pact["pcf"]["declaredUnitOfMeasurement"] = "ton kilometer"
    r = decl.read_declaration(as_json(pact), "t.json", "cradle_to_gate", "kg", call_llm=no_llm)
    assert r["unit_mismatch"] is True and r["fields"]["declared_unit"]["value"] == "tonne.km"


def test_rejects_json_that_is_not_pact():
    with pytest.raises(decl.DeclarationError, match="PACT"):
        decl.read_declaration(b'{"hello": 1}', "x.json", call_llm=no_llm)
    with pytest.raises(decl.DeclarationError, match="valid JSON"):
        decl.read_declaration(b"not json", "x.json", call_llm=no_llm)
    with pytest.raises(decl.DeclarationError, match=".pdf or a PACT .json"):
        decl.read_declaration(b"x", "x.xlsx", call_llm=no_llm)
    with pytest.raises(decl.DeclarationError, match="needs the AI"):
        decl.read_declaration(b"%PDF", "x.pdf")


def test_pact_listing_takes_the_first_footprint():
    r = decl.read_declaration(as_json({"data": [PACT_3]}), "list.json", call_llm=no_llm)
    assert r["fields"]["value_kgco2e"]["value"] == 8.4


EPD_TEXT = """ENVIRONMENTAL PRODUCT DECLARATION
Rolling oil RX-20   Issued by Lubri GmbH
Declared unit: 1 tonne of product
GWP-total (A1-A3, cradle to gate): 2,450 kg CO2-eq per declared unit
Standards: EN 15804+A2, ISO 14025. Characterisation: IPCC AR5 GWP100.
Reference period: 01.01.2023 – 31.12.2023. Valid from 15.03.2024 until 14.03.2029.
Verified by: EPD Hub Ltd (third-party)
"""

LLM_ANSWER = {
    "product": {"value": "Rolling oil RX-20", "confidence": 90, "quote": "Rolling oil RX-20"},
    "supplier": {"value": "Lubri GmbH", "confidence": 90, "quote": "Issued by Lubri GmbH"},
    "value": {"value": "2,450", "confidence": 88, "quote": "GWP-total (A1-A3): 2,450 kg CO2-eq per declared unit"},
    "value_unit": {"value": "kg CO2-eq", "confidence": 90, "quote": None},
    "per_quantity": {"value": 1, "confidence": 90, "quote": "Declared unit: 1 tonne of product"},
    "per_unit": {"value": "tonne", "confidence": 90, "quote": "Declared unit: 1 tonne of product"},
    "boundary": {"value": "A1-A3, cradle to gate", "confidence": 85, "quote": "GWP-total (A1-A3, cradle to gate)"},
    "standard": {"value": "EN 15804+A2, ISO 14025", "confidence": 80, "quote": "Standards: EN 15804+A2, ISO 14025"},
    "gwp": {"value": "IPCC AR5 GWP100", "confidence": 80, "quote": None},
    "reference_start": {"value": "2023-01-01", "confidence": 85, "quote": "Reference period: 01.01.2023"},
    "reference_end": {"value": "31.12.2023", "confidence": 85, "quote": None},
    "valid_from": {"value": "2024-03-15", "confidence": 80, "quote": None},
    "valid_to": {"value": "14 March 2029", "confidence": 80, "quote": None},
    "verifier": {"value": "EPD Hub Ltd", "confidence": 75, "quote": "Verified by: EPD Hub Ltd"},
}


def test_reads_a_pdf_through_the_ai_and_converts_per_tonne_to_per_kg():
    calls = []

    def llm(messages):
        calls.append(messages)
        return "```json\n" + json.dumps(LLM_ANSWER) + "\n```"

    r = decl.read_declaration(b"%PDF", "epd.pdf", "cradle_to_gate", "kg", call_llm=llm, extract_text=lambda c, n: EPD_TEXT)
    assert r["kind"] == "pdf" and r["ai_used"] is True
    assert calls[0][1]["content"] == EPD_TEXT
    f = r["fields"]
    assert f["value_kgco2e"]["value"] == pytest.approx(2.45) and f["value_kgco2e"]["by"] == "ai"
    assert f["value_kgco2e"]["confidence"] <= decl.AI_MAX_CONFIDENCE
    assert "converted from per t to per kg" in f["value_kgco2e"]["reason"]
    assert f["declared_unit"]["value"] == "kg"
    assert f["boundary"]["value"] == "cradle_to_gate"
    assert f["standard"]["value"] == "en15804"
    assert f["gwp_set"]["value"] == "AR5"
    assert f["reference_end"]["value"] == "2023-12-31" and f["valid_to"]["value"] == "2029-03-14"
    assert f["verifier"]["value"] == "EPD Hub Ltd"
    assert r["boundary_mismatch"] is False and r["unit_mismatch"] is False and r["problems"] == []


def test_pdf_with_grave_boundary_and_no_unit_is_flagged():
    answer = json.loads(json.dumps(LLM_ANSWER))
    answer["boundary"]["value"] = "cradle-to-grave"
    answer["per_unit"]["value"] = None
    answer["value_unit"]["value"] = "t CO2e"
    answer["value"]["value"] = 2.45
    r = decl.read_declaration(b"%PDF", "epd.pdf", "cradle_to_gate", None, call_llm=lambda m: json.dumps(answer), extract_text=lambda c, n: "text")
    assert r["boundary_mismatch"] is True
    assert "No declared unit found" in r["problems"]
    assert r["fields"]["value_kgco2e"]["value"] == pytest.approx(2450) and r["fields"]["value_kgco2e"]["confidence"] <= 30
    assert "value_kgco2e" in r["low_confidence_fields"]


def test_pdf_ai_failures_are_errors_not_guesses():
    with pytest.raises(decl.DeclarationError, match="couldn't"):
        decl.read_declaration(b"%PDF", "epd.pdf", call_llm=lambda m: "no json here", extract_text=lambda c, n: "text")
    with pytest.raises(decl.DeclarationError, match="No text"):
        decl.read_declaration(b"%PDF", "epd.pdf", call_llm=no_llm, extract_text=lambda c, n: "   ")


def test_date_and_boundary_helpers():
    assert decl.to_iso_date("2024-01-05T00:00:00Z") == "2024-01-05"
    assert decl.to_iso_date("05/01/2024") == "2024-01-05"
    assert decl.to_iso_date("March 2024") == "2024-03-01"
    assert decl.to_iso_date("2024") == "2024-01-01"
    assert decl.to_iso_date("31/02/2024") is None and decl.to_iso_date("soon") is None
    assert decl.boundary_from("Cradle–to–gate (A1–A3)") == "cradle_to_gate"
    assert decl.boundary_from("A1-C4") == "cradle_to_grave"
    assert decl.boundary_from("gate to gate") == "gate_to_gate"
    assert decl.boundary_from("whole life") is None
    assert decl.standard_from("ISO 14067:2018") == "iso14067" and decl.standard_from("PAS2050") == "pas2050"


# ----------------------------------------------------------------- endpoint ---

PCF_SUPERADMIN, PCF_ADMIN = 65, 66
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

    monkeypatch_module.setattr(pcf_api, "_llm", lambda messages: json.dumps(LLM_ANSWER))
    monkeypatch_module.setattr(pcf_api, "_extract_text", lambda content, name: EPD_TEXT)
    ensure_pcf_evidence_table()
    with throwaway_db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
              (65, 'PCF Root 3', 'pcf-root3@example.invalid', 'x', 'Superadmin', NULL),
              (66, 'PCF Admin 3', 'pcf-admin3@example.invalid', 'x', 'Admin', 1)
            """
        )
    yield TestClient(app)
    with throwaway_db.cursor() as cur:
        cur.execute('DELETE FROM "user" WHERE user_id IN (65, 66)')
        cur.execute("DELETE FROM pcf_evidence WHERE kind = 'declaration'")


URL = "/v1/pcf/parse-declaration"


def upload(content=None, name="ingot.json", **form):
    return {"files": {"file": (name, content if content is not None else as_json(PACT_3))}, "data": form}


def test_parse_declaration_needs_a_manager_or_superadmin(client):
    assert client.post(URL, **upload()).status_code == 401
    assert client.post(URL, headers=bearer(USER_A), **upload()).status_code == 403
    assert client.post(URL, headers=bearer(PCF_ADMIN), **upload()).status_code == 403
    for who in (MANAGER_A, PCF_SUPERADMIN):
        r = client.post(URL, headers=bearer(who), **upload(keep_evidence="false"))
        assert r.status_code == 200, r.text
        assert r.json()["fields"]["value_kgco2e"]["value"] == 8.4 and r.json()["evidence"] is None


def test_parse_declaration_keeps_the_file_as_evidence(client, throwaway_db):
    r = client.post(URL, headers=bearer(MANAGER_A), **upload(study_id="7", expected_unit="kg"))
    assert r.status_code == 200, r.text
    ev = r.json()["evidence"]
    assert ev and ev["document_name"] == "ingot.json" and ev["stored"] in ("cloud", "local")
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT kind, pcf_study_id, uploaded_by, file_size FROM pcf_evidence WHERE id = %s", (ev["id"],))
        assert cur.fetchone() == ("declaration", 7, MANAGER_A, len(as_json(PACT_3)))


def test_parse_declaration_reads_a_pdf_and_validates_inputs(client):
    r = client.post(URL, headers=bearer(MANAGER_A), **upload(b"%PDF", "epd.pdf", keep_evidence="false", expected_unit="kg"))
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "pdf" and r.json()["fields"]["value_kgco2e"]["value"] == pytest.approx(2.45)
    assert client.post(URL, headers=bearer(MANAGER_A), **upload(b"x", "bom.xlsx")).status_code == 400
    assert client.post(URL, headers=bearer(MANAGER_A), **upload(b"{}", keep_evidence="false")).status_code == 422
    assert client.post(URL, headers=bearer(MANAGER_A), **upload(expected_boundary="cradle_to_moon", keep_evidence="false")).status_code == 422
    assert client.post(URL, headers=bearer(MANAGER_A), **upload(expected_unit="pcs", keep_evidence="false")).status_code == 422
    r = client.post(URL, headers=bearer(MANAGER_A), **upload(expected_boundary="cradle_to_grave", keep_evidence="false"))
    assert r.status_code == 200 and r.json()["boundary_mismatch"] is True


def test_pact_default_validity_survives_a_leap_day_reference_end():
    pact = dict(PACT_3)
    pact["pcf"] = dict(PACT_3["pcf"], referencePeriodEnd="2024-02-29T00:00:00Z")
    pact.pop("validityPeriodEnd", None)
    r = decl.read_declaration(as_json(pact), "leap.json", "cradle_to_gate", "kg", call_llm=no_llm)
    assert r["fields"]["valid_to"]["value"] == "2027-02-28"
    assert r["fields"]["valid_to"]["confidence"] == 70


def test_a_failing_llm_call_is_a_declaration_error_not_a_crash():
    def boom(_messages):
        raise RuntimeError("connection reset")

    with pytest.raises(decl.DeclarationError):
        decl.read_declaration(b"%PDF", "epd.pdf", "cradle_to_gate", "kg", call_llm=boom, extract_text=lambda c, n: "some text")
    with pytest.raises(decl.DeclarationError):
        decl.read_declaration(b"%PDF", "epd.pdf", "cradle_to_gate", "kg", call_llm=lambda m: None, extract_text=lambda c, n: "some text")
