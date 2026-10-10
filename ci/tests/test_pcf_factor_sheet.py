"""E2: the PCF material-factor sheet reader.

Sheets are built in memory; the LLM is a stub (CI never calls it). The
endpoint tests run against the throwaway DB for the sign-in checks.
"""
import datetime as dt
import io
import json
import os
import time

import jwt
import openpyxl
import pytest

from app.services import pcf_sheet
from app.services.pcf_units import PcfUnit, UnknownUnit, co2e_multiplier, to_pcf_unit


def xlsx(*sheets):
    """xlsx bytes from (title, rows) pairs."""
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


# ------------------------------------------------------------------- units ---

@pytest.mark.parametrize(
    "written, unit, per",
    [
        ("kg", "kg", 1.0),
        ("kgs", "kg", 1.0),
        ("kgCO2e/kg", "kg", 1.0),
        ("kg CO2e per tonne", "t", 1.0),
        ("Tonnes", "t", 1.0),
        ("g", "kg", 0.001),
        ("MWh", "kWh", 1000.0),
        ("per kWh", "kWh", 1.0),
        ("tkm", "tonne.km", 1.0),
        ("kgCO2e/t.km", "tonne.km", 1.0),
        ("kg.km", "tonne.km", 0.001),
        ("m²", "m2", 1.0),
        ("pcs", "unit", 1.0),
        ("Each", "unit", 1.0),
    ],
)
def test_units_map_to_pcf_units(written, unit, per):
    u = to_pcf_unit(written)
    assert isinstance(u, PcfUnit), u
    assert u.unit == unit
    assert u.per_written == pytest.approx(per)


@pytest.mark.parametrize("written", ["ton", "tons", "litre", "USD", "", "kgCO2e"])
def test_units_that_cannot_be_placed_are_never_guessed(written):
    u = to_pcf_unit(written)
    assert isinstance(u, UnknownUnit)
    assert u.reason


def test_co2e_mass_in_a_header():
    assert co2e_multiplier("tCO2e/t")[0] == 1000
    assert co2e_multiplier("g CO2-eq per kg")[0] == 0.001
    assert co2e_multiplier("kg CO₂e / kg")[0] == 1
    assert co2e_multiplier("Value") is None


# ------------------------------------------------------------------- sheet ---

TEMPLATE = [
    ["name", "material_group", "unit", "value_kgco2e", "geography", "gwp_set", "source", "source_year", "licence", "recycled_variant", "valid_from"],
    ["Primary aluminium ingot", "aluminium", "kg", 16.1, "GCC", "AR6", "IAI", 2023, "open", "no", dt.datetime(2024, 1, 1)],
    ["Recycled aluminium", "aluminium", "kg", 0.6, "Global", "AR6", "IAI", 2023, "open", "yes", None],
]


def test_template_sheet_reads_exactly_without_ai():
    out = pcf_sheet.read_factor_sheet(xlsx(("Factors", TEMPLATE)), "f.xlsx", call_llm=no_llm)
    assert out["header_line"] == 1
    assert out["ai_used"] is False
    assert out["rows_with_problems"] == 0
    assert {f: m["confidence"] for f, m in out["mapping"].items()}["value_kgco2e"] == 95
    first, second = out["rows"]
    assert first["line"] == 2
    assert first["values"] == {
        "name": "Primary aluminium ingot",
        "material_group": "aluminium",
        "unit": "kg",
        "value_kgco2e": 16.1,
        "geography": "GCC",
        "gwp_set": "AR6",
        "source": "IAI",
        "source_year": 2023,
        "licence": "open",
        "recycled_variant": False,
        "valid_from": "2024-01-01",
    }
    assert first["confidence"] == 95
    assert first["reason"] == "Columns matched by header"
    assert second["values"]["recycled_variant"] is True


def test_title_banner_tonnes_and_groups_from_names():
    rows = [
        ["Supplier factor list 2024"],
        [],
        ["Material description", "Region", "GWP (t CO2e / t)", "Ref year"],
        ["EC grade Al rod 9.5mm", "BH", 8.6, "2023"],
        ["XLPE compound", "EU", 2.4, 2022],
        ["Wooden drum", "BH", 0.31, 2022],
        ["Mystery input", "BH", 1.0, 2022],
    ]
    out = pcf_sheet.read_factor_sheet(xlsx(("List", rows)), "s.xlsx", call_llm=no_llm)
    assert out["header_line"] == 3
    assert out["mapping"]["name"]["header"] == "Material description"
    assert out["mapping"]["value_kgco2e"]["header"] == "GWP (t CO2e / t)"
    assert out["sheet_unit"]["unit"] == "t"
    rod, xlpe, drum, unknown = out["rows"]
    # tCO2e per t → kgCO2e per t: ×1000, unit t.
    assert rod["values"]["value_kgco2e"] == pytest.approx(8600)
    assert rod["values"]["unit"] == "t"
    assert rod["values"]["source_year"] == 2023
    assert rod["values"]["material_group"] == "aluminium"
    assert "multiplied by 1,000" in rod["reason"]
    assert xlpe["values"]["material_group"] == "polymer"
    assert drum["values"]["material_group"] == "packaging"
    assert rod["confidence"] == 65  # group came from the name
    assert unknown["problems"] and "Group" in unknown["problems"][0]


def test_unit_column_with_conversions_and_unknown_units():
    rows = [
        ["Material", "Group", "UOM", "Factor"],
        ["Grid electricity BH", "energy", "kgCO2e/MWh", 720],
        ["Copper cathode", "copper", "g", 0.004],
        ["Steel wire", "steel", "ton", 2100],
    ]
    out = pcf_sheet.read_factor_sheet(xlsx(("A", rows)), "u.xlsx", call_llm=no_llm)
    grid, cu, steel = out["rows"]
    assert grid["values"]["unit"] == "kWh"
    assert grid["values"]["value_kgco2e"] == pytest.approx(0.72)
    assert cu["values"]["unit"] == "kg"
    assert cu["values"]["value_kgco2e"] == pytest.approx(4.0)
    assert steel["problems"] and "short ton" in steel["problems"][0]
    assert out["rows_with_problems"] == 1


def test_material_group_header_is_not_taken_as_the_name():
    rows = [["Material group", "Material", "Unit", "kg CO2e"], ["Metals - Copper", "Cu rod", "kg", 3.9]]
    out = pcf_sheet.read_factor_sheet(xlsx(("A", rows)), "g.xlsx", call_llm=no_llm)
    assert out["mapping"]["material_group"]["header"] == "Material group"
    assert out["mapping"]["name"]["header"] == "Material"
    row = out["rows"][0]
    assert row["values"]["material_group"] == "copper"
    assert "read as copper" in row["reason"]


def test_picks_the_sheet_that_has_factors():
    content = xlsx(("Read me", [["This workbook lists factors"], ["See next sheet"]]), ("Data", TEMPLATE))
    out = pcf_sheet.read_factor_sheet(content, "w.xlsx", call_llm=no_llm)
    assert out["sheet_names"] == ["Read me", "Data"]
    assert out["sheet_name"] == "Data"
    with pytest.raises(pcf_sheet.SheetError):
        pcf_sheet.read_factor_sheet(content, "w.xlsx", sheet_name="Nope", call_llm=no_llm)


def test_csv_with_semicolons_and_decimal_commas():
    text = "Material;Group;Unit;Value\nPVC compound;polymer;kg;2,41\nPallet;packaging;pcs;1,000\n"
    out = pcf_sheet.read_factor_sheet(text.encode(), "f.csv", call_llm=no_llm)
    pvc, pallet = out["rows"]
    assert pvc["values"]["value_kgco2e"] == pytest.approx(2.41)
    assert pallet["values"]["value_kgco2e"] == pytest.approx(1000)
    assert pallet["values"]["unit"] == "unit"


def test_ai_places_columns_headers_cannot():
    rows = [["Col A", "Col B", "Col C"], ["Aluminium billet", "kg", 9.1]]
    calls = []

    def llm(messages):
        calls.append(messages)
        return json.dumps({"mapping": {
            "name": {"header": "Col A", "confidence": 88, "reason": "Text naming materials"},
            "value_kgco2e": {"header": "Col C", "confidence": 99, "reason": "Small decimals"},
            "unit": {"header": "Col B", "confidence": 90, "reason": "not asked for"},
        }})

    out = pcf_sheet.read_factor_sheet(xlsx(("A", rows)), "a.xlsx", call_llm=llm)
    assert len(calls) == 1
    assert out["ai_used"] is True
    assert out["mapping"]["name"] == {"header": "Col A", "confidence": 88, "reason": "Text naming materials", "by": "ai"}
    assert out["mapping"]["value_kgco2e"]["confidence"] == 90  # AI is capped below an exact header
    assert "unit" not in out["mapping"]  # only the fields it was asked for
    row = out["rows"][0]
    assert row["values"]["name"] == "Aluminium billet"
    assert row["reason"].startswith("AI matched")
    assert row["problems"] == ["Unit is empty"]


def test_ai_failure_is_a_warning_not_an_error():
    def broken(_m):
        raise RuntimeError("down")

    out = pcf_sheet.read_factor_sheet(xlsx(("A", [["Col A", "Col B"], ["x", 1]])), "a.xlsx", call_llm=broken)
    assert out["ai_used"] is False
    assert any("unavailable" in w for w in out["warnings"])
    assert all("Name is empty" in r["problems"] for r in out["rows"])


def test_person_mapping_overrides_and_reassigns_a_header():
    rows = [["Material", "Factor", "Alt factor", "Unit", "Group"], ["Cu", 3.0, 4.5, "kg", "copper"]]
    out = pcf_sheet.read_factor_sheet(
        xlsx(("A", rows)), "o.xlsx", mapping_override={"value_kgco2e": "Alt factor", "material_group": None, "geography": "Missing"}, call_llm=no_llm
    )
    assert out["mapping"]["value_kgco2e"] == {"header": "Alt factor", "confidence": 100, "reason": "You picked “Alt factor”", "by": "person"}
    assert "material_group" not in out["mapping"]
    assert out["rows"][0]["values"]["value_kgco2e"] == 4.5
    # Group column unpicked: the group comes from the name “Cu”, at lower confidence.
    assert out["rows"][0]["values"]["material_group"] == "copper"
    assert out["rows"][0]["confidence"] == 65
    assert any("no column “Missing”" in w for w in out["warnings"])


def test_text_value_column_lowers_confidence():
    rows = [["Material", "Unit", "Group", "Value"], ["Cu", "kg", "copper", "see note"], ["Al", "kg", "aluminium", "n/a"], ["Fe", "kg", "steel", 2.0]]
    out = pcf_sheet.read_factor_sheet(xlsx(("A", rows)), "t.xlsx", call_llm=no_llm)
    assert out["mapping"]["value_kgco2e"]["confidence"] == 40
    assert out["low_confidence_rows"] == 3
    assert any("not numbers" in w for w in out["warnings"])


def test_empty_and_oversized_sheets_are_refused():
    with pytest.raises(pcf_sheet.SheetError):
        pcf_sheet.read_factor_sheet(xlsx(("A", [["Material", "Value"]])), "e.xlsx", call_llm=no_llm)
    with pytest.raises(pcf_sheet.SheetError):
        pcf_sheet.read_factor_sheet(b"x", "e.xls", call_llm=no_llm)
    big = [["Material", "Value"]] + [[f"m{i}", 1] for i in range(pcf_sheet.MAX_ROWS + 1)]
    with pytest.raises(pcf_sheet.SheetError, match="at most"):
        pcf_sheet.read_factor_sheet(xlsx(("A", big)), "b.xlsx", call_llm=no_llm)


# ---------------------------------------------------------------- endpoint ---

PCF_SUPERADMIN, PCF_ADMIN = 61, 62
USER_A, MANAGER_A = 1, 2  # reference_data.sql


def bearer(user_id):
    payload = {"userId": user_id, "role": "x", "siteId": None, "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(payload, os.environ["AUTH_JWT_SECRET"], algorithm="HS256")}


@pytest.fixture(scope="module")
def client(throwaway_db, monkeypatch_module):
    from fastapi.testclient import TestClient

    from app.api import pcf as pcf_api
    from app.main import app

    monkeypatch_module.setattr(pcf_api, "_llm", no_llm)
    with throwaway_db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
              (61, 'PCF Root', 'pcf-root@example.invalid', 'x', 'Superadmin', NULL),
              (62, 'PCF Admin', 'pcf-admin@example.invalid', 'x', 'Admin', 1)
            """
        )
    yield TestClient(app)
    with throwaway_db.cursor() as cur:
        cur.execute('DELETE FROM "user" WHERE user_id IN (61, 62)')


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


URL = "/v1/pcf/material-factors/parse-excel"


def upload(content=None, name="f.xlsx", **form):
    return {"files": {"file": (name, content if content is not None else xlsx(("A", TEMPLATE)))}, "data": form}


def test_endpoint_needs_a_manager_or_superadmin(client):
    assert client.post(URL, **upload()).status_code == 401
    assert client.post(URL, headers=bearer(USER_A), **upload()).status_code == 403
    assert client.post(URL, headers=bearer(PCF_ADMIN), **upload()).status_code == 403
    assert client.post(URL, headers={"X-Service-Key": os.environ["AI_SERVICE_KEY"]}, **upload()).status_code == 403
    for who in (MANAGER_A, PCF_SUPERADMIN):
        r = client.post(URL, headers=bearer(who), **upload())
        assert r.status_code == 200, r.text
        assert r.json()["total_rows"] == 2


def test_endpoint_reads_mapping_and_refuses_bad_input(client):
    h = bearer(MANAGER_A)
    r = client.post(URL, headers=h, **upload(mapping=json.dumps({"geography": None})))
    assert r.status_code == 200
    assert "geography" not in r.json()["mapping"]
    assert client.post(URL, headers=h, **upload(mapping="{")).status_code == 422
    assert client.post(URL, headers=h, **upload(mapping='["x"]')).status_code == 422
    assert client.post(URL, headers=h, **upload(name="f.pdf")).status_code == 400
    assert client.post(URL, headers=h, **upload(content=b"", name="f.csv")).status_code == 400
    r = client.post(URL, headers=h, **upload(content=xlsx(("A", [["Material", "Value"]]))))
    assert r.status_code == 422
    assert "no data rows" in r.json()["detail"]
