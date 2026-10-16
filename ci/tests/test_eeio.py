"""S3-12 · EEIO sector suggestion (app/scope3/eeio.py, POST /v1/scope3/eeio-sector).

The prompt holds only the line's own text (description and supplier, no other
line, no amount, no currency question); a low model confidence comes back as
is; codes outside the list are dropped; at most 3 per line; without a model
the word match answers below 80; the route is ESG-lite's service key only.
No database and no real model call.
"""
import json
import os

import pytest

from app.scope3 import eeio

SECTORS = [
    {"code": "481000", "name": "Air transportation (NAICS 481000)"},
    {"code": "482000", "name": "Rail transportation (NAICS 482000)"},
    {"code": "721000", "name": "Accommodation (NAICS 721000)"},
    {"code": "331110", "name": "Iron and steel mills and ferroalloy manufacturing (NAICS 331110)"},
    {"code": "541100", "name": "Legal services (NAICS 541100)"},
]


def request(*lines):
    return eeio.EeioSectorRequest.model_validate({"lines": list(lines), "sectors": SECTORS})


LINE_A = {"description": "Air travel, return flights Delhi to Mumbai", "supplier": "Skyways Travel Pvt", "amount": 84250.5, "currency": "INR"}
LINE_B = {"description": "Hot rolled steel coil", "supplier": "Secret Metals Ltd", "amount": 991234, "currency": "USD"}


class FakeModel:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


def test_the_prompt_holds_only_the_lines_own_text():
    model = FakeModel([{"suggestions": [{"code": "481000", "confidence": 92}]}, {"suggestions": [{"code": "331110", "confidence": 88}]}])
    eeio.suggest(request(LINE_A, LINE_B), ask=model)
    assert len(model.calls) == 2, "one call per line, never a batch of lines"
    (sys_a, user_a), (_sys_b, user_b) = model.calls
    assert "Air travel, return flights Delhi to Mumbai" in user_a and "Skyways Travel Pvt" in user_a
    assert "Hot rolled steel coil" not in user_a and "Secret Metals" not in user_a, "another line's text never goes in"
    assert "Skyways" not in user_b and "flights" not in user_b
    for text in (user_a, user_b):
        assert "84250" not in text and "991234" not in text, "the amount is not sent"
        assert "INR" not in text and "USD" not in text, "the currency is not sent"
    assert "currency" not in sys_a.lower(), "the model is never asked about a currency"
    # Only shortlisted sectors (code and name) follow the line.
    assert "481000\tAir transportation (NAICS 481000)" in user_a


def test_low_confidence_is_returned_as_is_and_codes_outside_the_list_are_dropped():
    model = FakeModel([{"suggestions": [{"code": "999999", "confidence": 99}, {"code": "481000", "confidence": 41}, {"code": "481000", "confidence": 40}, {"code": "482000", "confidence": 140}]}])
    out = eeio.suggest(request(LINE_A), ask=model)
    assert [s.model_dump() for s in out.lines[0].suggestions] == [{"code": "481000", "confidence": 41}, {"code": "482000", "confidence": 100}]


def test_at_most_three_per_line_in_line_order():
    answer = {"suggestions": [{"code": c["code"], "confidence": 50} for c in SECTORS]}
    out = eeio.suggest(request(LINE_A, LINE_B), ask=FakeModel([answer, {"suggestions": []}]))
    assert len(out.lines) == 2
    assert len(out.lines[0].suggestions) == 3
    assert out.lines[1].suggestions == [], "an empty answer means no sector fits"


def test_without_a_model_or_when_it_fails_the_word_match_answers_below_80():
    out = eeio.suggest(request(LINE_A, LINE_B), ask=None) if eeio.configured_ask() is None else None
    assert out is not None, "CI runs with MAPPING_MODEL_PROVIDER=none"
    assert out.lines[0].suggestions[0].code == "481000"
    assert out.lines[1].suggestions[0].code == "331110"
    assert all(s.confidence < 80 for line in out.lines for s in line.suggestions)
    failing = eeio.suggest(request(LINE_A), ask=FakeModel([RuntimeError("busy")]))
    assert failing.lines[0].suggestions[0].code == "481000"
    assert failing.lines[0].suggestions[0].confidence <= eeio.FALLBACK_CONFIDENCE_CAP
    garbled = eeio.suggest(request(LINE_A), ask=FakeModel(["not json at all"]))
    assert garbled.lines[0].suggestions[0].code == "481000"


def test_the_request_is_validated():
    with pytest.raises(Exception):
        eeio.EeioSectorRequest.model_validate({"lines": [], "sectors": SECTORS})
    with pytest.raises(Exception):
        eeio.EeioSectorRequest.model_validate({"lines": [{**LINE_A, "currency": "rupees"}], "sectors": SECTORS})
    with pytest.raises(Exception):
        eeio.EeioSectorRequest.model_validate({"lines": [LINE_A], "sectors": SECTORS + [SECTORS[0]]})


def test_logs_carry_counts_only(caplog):
    caplog.set_level("INFO", logger="app.scope3.eeio")
    eeio.suggest(request(LINE_A), ask=FakeModel([RuntimeError("busy")]))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "1 line(s)" in text
    assert "Delhi" not in text and "Skyways" not in text


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_route_is_esg_lites_service_key_only(client, monkeypatch):
    body = {"lines": [LINE_A], "sectors": SECTORS}
    assert client.post("/v1/scope3/eeio-sector", json=body).status_code == 401
    assert client.post("/v1/scope3/eeio-sector", headers={"X-Service-Key": "wrong"}, json=body).status_code == 401
    service = {"X-Service-Key": os.environ["AI_SERVICE_KEY"]}
    assert client.post("/v1/scope3/eeio-sector", headers=service, json={}).status_code == 422
    monkeypatch.setattr(eeio, "configured_ask", lambda: None)
    r = client.post("/v1/scope3/eeio-sector", headers=service, json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert list(data) == ["lines"]
    assert data["lines"][0]["suggestions"][0]["code"] == "481000"
    assert set(data["lines"][0]["suggestions"][0]) == {"code", "confidence"}
