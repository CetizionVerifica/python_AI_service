"""XI2 2/2 model layer with a fake model: it is called only for unresolved
parts, fills them with source "llm" and a capped confidence, never overrides
the rules, drops every name outside the context as NOT_IN_CONTEXT, and a
model failure leaves the rules proposal intact. Plus the adapter wiring."""
import pytest

from app.schemas.mapping import MappingProposal
from app.services.mapping import model as model_mod
from app.services.mapping.engine import propose
from app.services.mapping.model import ModelAnswer, ModelError, configured_model
from app.services.workbook import read_workbook
from ci.tests.conftest import FIXTURES
from ci.tests.mapping_fixtures import ctx, lookup

XI1 = FIXTURES / "xi1"


class FakeModel:
    name = "fake-model"

    def __init__(self, answer=None, error=None):
        self.answer = answer or {}
        self.error = error
        self.calls = []

    def propose(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise ModelError(self.error)
        return ModelAnswer.model_validate(self.answer)


def run(name, model, index=0):
    wb = read_workbook(XI1 / name)
    p = propose(wb, 1, ctx(), [], lookup(), model=model)
    return p, p.sheets[index]


def test_no_call_when_the_rules_resolved_everything():
    model = FakeModel()
    p, _ = run("12-large-ledger.csv", model)
    assert model.calls == [] and p.model.calls == 0 and p.model.name == "fake-model"


def test_model_fills_the_unresolved_parts_with_llm_source_and_capped_confidence():
    model = FakeModel(
        {
            "category": {"kind": "fixed", "category_id": 5, "confidence": 1.0},
            "fields": [{"key": "activity_name", "column": "Site", "confidence": 0.9}, {"key": "activity_value", "column": "Diesel / Litre", "confidence": 0.9}, {"key": "activity_unit", "column": "Period", "confidence": 0.5}],
            "activities": [],
        }
    )
    p, s = run("09-two-row-merged-header.xlsx", model)
    assert p.model.calls == 1
    assert s.category.id == 5 and s.category.source == "llm" and s.category.confidence == 0.95
    f = {x.key: x for x in s.fields}
    # The rules had already claimed the value column and the period: the model does not override them.
    assert f["activity_value"].column == "Electricity / kWh" and f["activity_value"].source == "rules"
    assert s.period.column == "Period"
    assert f["activity_name"].column == "Site" and f["activity_name"].source == "llm"
    assert s.source == "mixed"
    MappingProposal.model_validate(p.model_dump())


def test_names_outside_the_context_are_dropped_and_listed():
    model = FakeModel(
        {
            "site_column": "Plant",
            "category": {"kind": "column", "column": "Unit", "values": [{"text": "litre", "category_id": 99, "confidence": 0.9}, {"text": "kWh", "category_id": 5, "confidence": 0.9}]},
            "period": {"kind": "column", "column": "Nowhere", "confidence": 0.9},
            "fields": [{"key": "supplier", "column": "Ghost column", "confidence": 0.9}, {"key": "not_a_field", "column": "Unit", "confidence": 0.9}],
            "activities": [{"sheet_text": "Petrol", "category_id": 4, "factor_name": "Zed secret clinker", "confidence": 0.9}, {"sheet_text": "Grid electricity", "category_id": 5, "factor_name": "grid electricity", "confidence": 0.8}],
        }
    )
    _, s = run("07-months-as-columns.xlsx", model)
    assert set(s.not_in_context) >= {"Plant", "litre", "Nowhere", "Ghost column", "Zed secret clinker"}
    assert s.category.kind == "column" and [(v.text, v.id) for v in s.category.values] == [("litre", None), ("kWh", 5)]
    assert s.period.kind == "months_as_columns"  # the rules' period was kept; the model's bad column was dropped
    assert {a.sheet_text: a.factor_name for a in s.activities}["Grid electricity"] == "Grid electricity"
    assert {a.sheet_text: a.factor_name for a in s.activities}["Petrol"] is None
    assert "not_a_field" not in {x.key for x in s.fields}
    assert "Zed secret clinker" not in [a.factor_name for a in s.activities]


def test_a_model_failure_leaves_the_rules_proposal_and_counts_the_call():
    with_model, s1 = run("07-months-as-columns.xlsx", FakeModel(error="down"))
    without, s2 = run("07-months-as-columns.xlsx", None)
    assert with_model.model.calls == 1 and without.model.calls == 0
    assert s1.model_dump(exclude={"source"}) == s2.model_dump(exclude={"source"})


def test_profile_hit_makes_no_model_call():
    _, first = run("07-months-as-columns.xlsx", None)
    sheet = first.model_dump()
    sheet["category"] = {"kind": "fixed", "id": 4, "label": "Mobile combustion", "confidence": 1.0, "source": "profile"}
    sheet["activities"] = [{"sheet_text": t, "category_id": 4, "factor_name": t, "confidence": 1.0, "source": "profile", "save_mapping": False} for t in ("Diesel", "Petrol")] + [{"sheet_text": "Grid electricity", "category_id": 4, "factor_name": "CNG", "confidence": 1.0, "source": "profile", "save_mapping": False}]
    profile = {"id": "0f6a6e3c-1111-4aaa-8bbb-ccccccccdddd", "company_id": 1, "site_id": 2, "fingerprint": sheet["fingerprint"], "sheet_proposal": sheet, "uses": 1, "created_at": "x", "updated_at": "x"}
    model = FakeModel()
    wb = read_workbook(XI1 / "07-months-as-columns.xlsx")
    p = propose(wb, 1, ctx(), [profile], lookup(), model=model)
    assert p.model.cached_profile_hits == 1 and p.model.calls == 0 and model.calls == []


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def test_configured_model_is_none_without_a_key_or_with_provider_none(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "anthropic")
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "")
    assert configured_model() is None
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "none")
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "sk-test")
    assert configured_model() is None


def test_configured_model_picks_the_anthropic_adapter_with_a_key(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "anthropic")
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "sk-test-not-real")
    m = configured_model()
    assert isinstance(m, model_mod.AnthropicMappingModel) and m.name == model_mod.settings.ANTHROPIC_MAPPING_MODEL


def test_configured_model_picks_openrouter(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "openrouter")
    assert isinstance(configured_model(), model_mod.OpenRouterMappingModel)


def test_openrouter_adapter_parses_json_and_refuses_bad_shapes(monkeypatch):
    from app.services import llm

    monkeypatch.setattr(llm, "call_openrouter_within", lambda messages, budget: '```json\n{"category": {"kind": "fixed", "category_id": 3, "confidence": 0.7}}\n```')
    answer = model_mod.OpenRouterMappingModel(model="test").propose("s", "u")
    assert answer.category.category_id == 3
    monkeypatch.setattr(llm, "call_openrouter_within", lambda messages, budget: '{"category": {"kind": "weird"}}')
    with pytest.raises(ModelError):
        model_mod.OpenRouterMappingModel(model="test").propose("s", "u")
    monkeypatch.setattr(llm, "call_openrouter_within", lambda messages, budget: (_ for _ in ()).throw(llm.LLMError("busy")))
    with pytest.raises(ModelError):
        model_mod.OpenRouterMappingModel(model="test").propose("s", "u")
