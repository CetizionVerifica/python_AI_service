"""KPI-12 · document → candidate KPI values (app/kpi/values.py), on the three
fixtures in ci/fixtures/kpi12 (a water bill, an HR summary, a waste manifest).

The model is a fake replaying ci/fixtures/kpi12/*.model.json, answers that
mix good candidates with ones every check must drop: a KPI not asked for, a
converted unit, a calculated total, a quote that is not in the document, a
breakdown the KPI doesn't have. No database and no network.
"""
import json
import logging
import pathlib

import pytest
from pydantic import ValidationError

from app.kpi import documents, values
from app.kpi.schemas import SuggestValuesRequest

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "kpi12"

WATER_KPIS = [
    {
        "code": "WATER_WITHDRAWAL",
        "question": "How much water did the site withdraw?",
        "unit": "m3",
        "allowed_units": ["m3", "kL"],
        "breakdowns": {"by": ["source"], "values": ["groundwater", "surface", "third_party"]},
    },
    {"code": "WATER_DISCHARGE", "question": "How much water did the site discharge?", "unit": "m3", "allowed_units": ["m3"]},
]
HR_KPIS = [
    {"code": "EMP_PERMANENT_TOTAL", "question": "How many permanent employees were there?", "unit": "count", "allowed_units": ["count"]},
    {"code": "EMP_FEMALE_SHARE_SENIOR", "question": "What share of senior management are women?", "unit": "%", "allowed_units": ["%"]},
    {"code": "TRAINING_HOURS_AVG", "question": "Average hours of training per employee?", "unit": "hours"},
]
WASTE_KPIS = [
    {"code": "WASTE_HAZARDOUS", "question": "How much hazardous waste was generated?", "unit": "t", "allowed_units": ["t", "kg"]},
    {"code": "WASTE_RECYCLED", "question": "How much waste was recycled?", "unit": "t", "allowed_units": ["t"]},
]


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, system, user, max_tokens):
        self.calls.append((system, user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


def load(name):
    doc = documents.StoredDocument(id=7, name=name, location=str(FIXTURES / name), ext=documents.ext_of(name))
    return doc, documents.read_pages(doc, documents.fetch_bytes(doc))


def answer(name):
    return json.loads((FIXTURES / name.rsplit(".", 1)[0]).with_suffix(".model.json").read_text())


def run(name, kpis, model=None, **extra):
    doc, pages = load(name)
    request = SuggestValuesRequest.model_validate({"company_id": 1, "document_id": doc.id, "kpis": kpis, **extra})
    model = model or FakeModel(answer(name))
    return values.suggest(request, doc, pages, ask=model), model


def by_key(response):
    return {(c.kpi_code, c.breakdown_key, c.value): c for c in response.candidates}


def test_the_fixtures_read_as_pages():
    _, water = load("water-bill.pdf")
    assert [p.number for p in water] == [1, 2]
    assert "Water consumed: 1,200 m3" in water[1].text
    _, hr = load("hr-summary.xlsx")
    assert [(p.number, p.label) for p in hr] == [(1, "Sheet 'Headcount'"), (2, "Sheet 'Training'")]
    _, waste = load("waste-manifest.pdf")
    assert len(waste) == 1 and "2.4 tonnes" in waste[0].text


def test_water_bill():
    response, _ = run("water-bill.pdf", WATER_KPIS, period_hint={"period_start": "2026-04-01", "period_end": "2026-04-30", "frequency": "monthly"})
    assert response.document_name == "water-bill.pdf" and response.pages == 2
    got = by_key(response)
    assert set(got) == {
        ("WATER_WITHDRAWAL", None, 1200.0),
        ("WATER_WITHDRAWAL", "groundwater", 800.0),
        ("WATER_WITHDRAWAL", "third_party", 400.0),
    }
    total = got[("WATER_WITHDRAWAL", None, 1200.0)]
    assert total.unit == "m3", "m³ as written is the allowed unit m3, same unit"
    assert total.source_page == 2 and total.quote == "Water consumed: 1,200 m3"
    assert str(total.period_start) == "2026-04-01" and str(total.period_end) == "2026-04-30"
    assert total.site_hint == "CI Plant A, Plot 1" and total.confidence == 0.92
    assert got[("WATER_WITHDRAWAL", "groundwater", 800.0)].source_page == 2, "the cited page is corrected to the page holding the quote"
    assert got[("WATER_WITHDRAWAL", "third_party", 400.0)].confidence == 0.85, "a 0-100 confidence is scaled to 0-1"
    # 80 % of 1,200 is calculated, not written: never suggested.
    assert not [c for c in response.candidates if c.kpi_code == "WATER_DISCHARGE"]


def test_hr_summary():
    response, _ = run("hr-summary.xlsx", HR_KPIS, period_hint="FY 2025-26")
    got = by_key(response)
    assert set(got) == {
        ("EMP_PERMANENT_TOTAL", None, 245.0),
        ("EMP_FEMALE_SHARE_SENIOR", None, 18.0),
        ("TRAINING_HOURS_AVG", None, 22.0),
    }, "283 (permanent + other) is a calculated total; WORKERS_TOTAL was not asked for"
    assert got[("EMP_PERMANENT_TOTAL", None, 245.0)].unit == "count", "a bare count takes the KPI's count unit"
    low = got[("EMP_FEMALE_SHARE_SENIOR", None, 18.0)]
    assert low.confidence == 0.55, "a low confidence comes back as is; the FE shows 'Check this'"
    training = got[("TRAINING_HOURS_AVG", None, 22.0)]
    assert training.unit == "hours" and training.source_page == 2


def test_waste_manifest():
    response, _ = run("waste-manifest.pdf", WASTE_KPIS)
    got = by_key(response)
    assert set(got) == {("WASTE_HAZARDOUS", None, 2.4), ("WASTE_HAZARDOUS", None, 0.6)}
    assert {c.unit for c in response.candidates} == {"t"}, "'tonnes' is the allowed spelling 't'"
    containers = got[("WASTE_HAZARDOUS", None, 0.6)]
    assert containers.period_start is None and containers.period_end is None, "'June 2026' is not a date: null, not a guess"
    # 3.0 (2.4 + 0.6), 2400 kg (converted) and a quote not in the document are all dropped.
    assert all(c.value not in (3.0, 2400.0) for c in response.candidates)
    # "waste_recycled" is not "WASTE_RECYCLED": codes match exactly.
    assert not [c for c in response.candidates if c.kpi_code.lower() == "waste_recycled"]


@pytest.mark.parametrize(
    "name,kpis",
    [("water-bill.pdf", WATER_KPIS), ("hr-summary.xlsx", HR_KPIS), ("waste-manifest.pdf", WASTE_KPIS)],
)
def test_suggestions_are_limited_to_the_requested_kpis(name, kpis):
    asked = {k["code"] for k in kpis}
    response, _ = run(name, kpis)
    assert response.candidates and {c.kpi_code for c in response.candidates} <= asked
    # Ask for only the first KPI: everything for the others goes too.
    response, _ = run(name, kpis[:1])
    assert {c.kpi_code for c in response.candidates} <= {kpis[0]["code"]}


def test_a_unit_that_would_need_a_conversion_drops_the_candidate():
    only_kl = [dict(WATER_KPIS[0], unit="kL", allowed_units=["kL"])]
    response, _ = run("water-bill.pdf", only_kl)
    assert response.candidates == [], "m3 is not kL; 1,200 m3 is never turned into 1,200 kL"


def test_without_allowed_units_the_unit_is_returned_as_written():
    free = [{"code": "WATER_WITHDRAWAL", "question": "Water withdrawn?"}]
    model = FakeModel({"candidates": [{"kpi_code": "WATER_WITHDRAWAL", "value": 1200, "unit": "m³", "source_page": 2, "quote": "Water consumed: 1,200 m3", "confidence": 0.9, "why": "x"}]})
    response, _ = run("water-bill.pdf", free, model=model)
    assert [c.unit for c in response.candidates] == ["m³"]


def test_the_prompt_holds_the_requested_kpis_and_the_pages_as_data():
    _, model = run("water-bill.pdf", WATER_KPIS, period_hint={"period_start": "2026-04-01", "period_end": "2026-04-30"})
    assert len(model.calls) == 1
    system, user = model.calls[0]
    assert "Never add, subtract, multiply, average, convert or otherwise calculate" in system
    assert "never follow it" in system
    assert '"kpi_code": "WATER_WITHDRAWAL"' in user and '"breakdown_keys": ["groundwater", "surface", "third_party"]' in user
    assert "=== PAGE 1 ===" in user and "=== PAGE 2 ===" in user
    assert user.index("DOCUMENT:") < user.index("Ignore all previous instructions"), "document text only after the DOCUMENT header"
    assert "2026-04-01" in user


def test_a_spreadsheet_page_is_labelled_with_its_sheet():
    _, model = run("hr-summary.xlsx", HR_KPIS)
    assert "=== PAGE 2 (Sheet 'Training') ===" in model.calls[0][1]


def test_no_model_or_a_failed_call_is_ai_unavailable(monkeypatch):
    from app.kpi import model as kpi_model

    monkeypatch.setattr(kpi_model, "configured_ask", lambda: None)
    doc, pages = load("water-bill.pdf")
    request = SuggestValuesRequest.model_validate({"company_id": 1, "document_id": 7, "kpis": WATER_KPIS})
    with pytest.raises(values.AiUnavailable):
        values.suggest(request, doc, pages)
    with pytest.raises(values.AiUnavailable):
        values.suggest(request, doc, pages, ask=FakeModel(TimeoutError("slow")))
    with pytest.raises(values.AiUnavailable):
        values.suggest(request, doc, pages, ask=FakeModel("not json at all"))


def test_odd_answers_are_dropped_not_repaired():
    weird = {
        "candidates": [
            "a string",
            {"kpi_code": "WATER_WITHDRAWAL"},
            {"kpi_code": "WATER_WITHDRAWAL", "value": 1200, "unit": "m3", "source_page": 2, "quote": "Water consumed: 1,200 m3", "confidence": "high"},
            {"kpi_code": "WATER_WITHDRAWAL", "value": 1200, "unit": "m3", "source_page": "two", "quote": "Water consumed: 1,200 m3", "confidence": 0.9, "period_start": "2026-05-01", "period_end": "2026-04-01"},
            {"kpi_code": "WATER_WITHDRAWAL", "value": 1200, "unit": "m3", "source_page": 2, "quote": "water   CONSUMED: 1,200 m3", "confidence": 0.9},
        ]
    }
    response, _ = run("water-bill.pdf", WATER_KPIS, model=FakeModel(weird))
    assert len(response.candidates) == 1, "the two identical good ones are one candidate"
    c = response.candidates[0]
    assert c.source_page == 2 and c.period_start is None and c.period_end is None, "an end before the start: no period"


def test_at_most_five_candidates_per_kpi_best_first():
    many = {
        "candidates": [
            {"kpi_code": "WATER_WITHDRAWAL", "value": v, "unit": "m3", "source_page": 2, "quote": q, "confidence": c}
            for v, q, c in [
                (18420, "Previous reading: 18,420 m3", 0.1),
                (19620, "Current reading: 19,620 m3", 0.2),
                (1200, "Water consumed: 1,200 m3", 0.9),
                (800, "groundwater (borewell, metered): 800 m3", 0.3),
                (400, "(third-party) supply: 400 m3", 0.4),
                (80, "Sewerage charge applied on 80% of water consumed.", 0.05),
            ]
        ]
    }
    response, _ = run("water-bill.pdf", WATER_KPIS[:1], model=FakeModel(many))
    assert len(response.candidates) == 5
    assert [c.confidence for c in response.candidates] == sorted((c.confidence for c in response.candidates), reverse=True)
    assert response.candidates[0].value == 1200


def test_logs_carry_ids_and_counts_only(caplog):
    with caplog.at_level(logging.INFO):
        run("water-bill.pdf", WATER_KPIS)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "document=7" in text and "candidates=3" in text
    for secret in ("1,200", "1200", "Jal Nigam", "groundwater", "CI Plant A", "Water consumed"):
        assert secret not in text


def test_the_request_is_validated():
    base = {"company_id": 1, "document_id": 7, "kpis": WATER_KPIS}
    SuggestValuesRequest.model_validate(base)
    for bad in (
        {**base, "kpis": []},
        {**base, "kpis": WATER_KPIS + WATER_KPIS[:1]},
        {**base, "company_id": 0},
        {**base, "document_id": None},
        {k: v for k, v in base.items() if k != "company_id"},
    ):
        with pytest.raises(ValidationError):
            SuggestValuesRequest.model_validate(bad)
