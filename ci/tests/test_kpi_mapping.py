"""KPI-12 · custom KPI → candidate disclosures (app/kpi/mapping.py).

Only the catalogue ESG-lite sends exists: a code outside it is dropped, the
title and section returned are the catalogue's, at most 5 come back, and
without a model the word match answers below 0.6. No database, no network.
"""
import json
import logging

import pytest
from pydantic import ValidationError

from app.kpi import mapping
from app.kpi.schemas import SuggestMappingRequest

CATALOGUE = [
    {"framework": "GRI", "code": "303-3", "title": "Water withdrawal", "section": "GRI 303 Water and Effluents"},
    {"framework": "GRI", "code": "303-4", "title": "Water discharge", "section": "GRI 303 Water and Effluents"},
    {"framework": "GRI", "code": "303-5", "title": "Water consumption", "section": "GRI 303 Water and Effluents"},
    {"framework": "BRSR", "code": "P6-E3", "title": "Water withdrawal by source", "section": "Principle 6 · Essential"},
    {"framework": "ESRS", "code": "E3-4", "title": "Water consumption", "section": "E3 Water and marine resources"},
    {"framework": "GRI", "code": "305-1", "title": "Direct (Scope 1) GHG emissions", "section": "GRI 305 Emissions"},
    {"framework": "GRI", "code": "405-1", "title": "Diversity of governance bodies and employees", "section": "GRI 405 Diversity"},
    {"framework": "BRSR", "code": "P3-E1", "title": "Employee well-being measures", "section": "Principle 3 · Essential"},
]
KPI = {"name": "Borewell water drawn", "question": "How much groundwater did the site withdraw from its borewells?", "unit": "m3", "topic": "water"}


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, system, user, max_tokens):
        self.calls.append((system, user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


def request(**extra):
    return SuggestMappingRequest.model_validate({**KPI, "catalogue": CATALOGUE, **extra})


def test_codes_are_limited_to_the_catalogue():
    model = FakeModel(
        {
            "suggestions": [
                {"framework": "GRI", "code": "303-3", "why": "groundwater drawn is a water withdrawal", "confidence": 0.93},
                {"framework": "GRI", "code": "303-3a-ii", "why": "a sub-item the model knows from elsewhere", "confidence": 0.9},
                {"framework": "CDP", "code": "W1.2b", "why": "a framework the company did not enable", "confidence": 0.8},
                {"framework": "BRSR", "code": "303-3", "why": "right code, wrong framework", "confidence": 0.7},
                {"framework": "BRSR", "code": "P6-E3", "why": "withdrawal by source includes groundwater", "confidence": 88},
                {"framework": "GRI", "code": "303-3", "why": "repeat", "confidence": 0.5},
            ]
        }
    )
    out = mapping.suggest(request(), ask=model)
    assert [(s.framework, s.code) for s in out.suggestions] == [("GRI", "303-3"), ("BRSR", "P6-E3")]
    catalogue = {(d["framework"], d["code"]): d for d in CATALOGUE}
    for s in out.suggestions:
        d = catalogue[(s.framework, s.code)]
        assert s.title == d["title"] and s.section == d["section"], "title and section come from the catalogue"
        assert s.why and 0 <= s.confidence <= 1
    assert out.suggestions[1].confidence == 0.88


def test_the_model_sees_only_the_kpi_and_the_catalogue_shortlist():
    model = FakeModel({"suggestions": []})
    mapping.suggest(request(), ask=model)
    system, user = model.calls[0]
    assert "Choose only from the disclosures listed under CATALOGUE" in system
    assert "Borewell water drawn" in user and '"code": "303-3"' in user
    rows = json.loads(user.split("CATALOGUE:\n", 1)[1])
    assert {(r["framework"], r["code"]) for r in rows} <= {(d["framework"], d["code"]) for d in CATALOGUE}


def test_shortlist_ranks_water_first_and_caps_the_prompt():
    top = mapping.shortlist(request(), k=3)
    assert {d.code for d, _, _ in top} <= {"303-3", "303-5", "P6-E3", "E3-4", "303-4"}
    big = [{"framework": "GRI", "code": f"X-{i}", "title": f"Unrelated disclosure {i}"} for i in range(500)]
    model = FakeModel({"suggestions": []})
    mapping.suggest(SuggestMappingRequest.model_validate({**KPI, "catalogue": CATALOGUE + big}), ask=model)
    rows = json.loads(model.calls[0][1].split("CATALOGUE:\n", 1)[1])
    assert len(rows) == mapping.SHORTLIST_SIZE


def test_at_most_five():
    answer = {"suggestions": [{"framework": d["framework"], "code": d["code"], "why": "x", "confidence": 0.5} for d in CATALOGUE]}
    out = mapping.suggest(request(), ask=FakeModel(answer))
    assert len(out.suggestions) == 5


@pytest.mark.parametrize("model", [None, FakeModel(TimeoutError("slow")), FakeModel("no json"), FakeModel({"answer": "other shape"})])
def test_without_a_model_or_when_it_fails_the_word_match_answers_below_0_6(model, monkeypatch):
    from app.kpi import model as kpi_model

    monkeypatch.setattr(kpi_model, "configured_ask", lambda: None)
    out = mapping.suggest(request(), ask=model)
    assert out.suggestions, "the word match still suggests"
    assert out.suggestions[0].code in {"303-3", "P6-E3"}
    assert all(s.confidence <= mapping.FALLBACK_CONFIDENCE_CAP for s in out.suggestions)
    assert all("water" in s.why.lower() or "withdraw" in s.why.lower() or "groundwater" in s.why.lower() for s in out.suggestions[:1])
    assert len(out.suggestions) <= 5


def test_nothing_in_common_is_no_suggestion(monkeypatch):
    from app.kpi import model as kpi_model

    monkeypatch.setattr(kpi_model, "configured_ask", lambda: None)
    out = mapping.suggest(SuggestMappingRequest.model_validate({"name": "Quokka sightings", "catalogue": CATALOGUE}))
    assert out.suggestions == []


def test_the_request_is_validated():
    with pytest.raises(ValidationError):
        SuggestMappingRequest.model_validate({**KPI, "catalogue": []})
    with pytest.raises(ValidationError):
        SuggestMappingRequest.model_validate({**KPI, "catalogue": CATALOGUE + CATALOGUE[:1]})
    with pytest.raises(ValidationError):
        SuggestMappingRequest.model_validate({"catalogue": CATALOGUE})
    # The same code under two frameworks is two disclosures.
    SuggestMappingRequest.model_validate({**KPI, "catalogue": CATALOGUE + [dict(CATALOGUE[0], framework="ESRS")]})


def test_logs_carry_counts_only(caplog):
    with caplog.at_level(logging.INFO):
        mapping.suggest(request(company_id=5), ask=FakeModel({"suggestions": [{"framework": "GRI", "code": "303-3", "why": "x", "confidence": 0.9}]}))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "company=5" in text and "suggestions=1" in text
    assert "Borewell" not in text and "groundwater" not in text and "303-3" not in text
