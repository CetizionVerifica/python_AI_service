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
            "fields": [{"key": "activity_name", "column": "Diesel / INR", "confidence": 0.9}, {"key": "activity_value", "column": "Diesel / Litre", "confidence": 0.9}, {"key": "activity_unit", "column": "Period", "confidence": 0.5}],
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
    assert f["activity_name"].column == "Diesel / INR" and f["activity_name"].source == "llm"
    # "Period" is already the period column: the model may not make it the unit column too.
    assert f["activity_unit"].column is None
    assert s.source == "mixed"
    MappingProposal.model_validate(p.model_dump())


def test_names_outside_the_context_are_dropped_and_listed():
    model = FakeModel(
        {
            "site_column": "Plant",
            "category": {"kind": "column", "column": "Unit", "values": [{"text": "litre", "category_id": 99, "confidence": 0.9}, {"text": "kWh", "category_id": 5, "confidence": 0.9}]},
            "period": {"kind": "column", "column": "Nowhere", "confidence": 0.9},
            "fields": [{"key": "supplier", "column": "Ghost column", "confidence": 0.9}, {"key": "not_a_field", "column": "Unit", "confidence": 0.9}],
            "activities": [{"sheet_text": "Petrol", "category_id": 5, "factor_name": "Zed secret clinker", "confidence": 0.9}, {"sheet_text": "Grid electricity", "category_id": 5, "factor_name": "grid electricity", "confidence": 0.8}],
        }
    )
    _, s = run("07-months-as-columns.xlsx", model)
    assert set(s.not_in_context) >= {"Plant", "category 99", "Nowhere", "Ghost column", "field not_a_field", "Zed secret clinker"}
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


def test_auto_provider_prefers_openrouter_then_anthropic_then_rules(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "auto")
    monkeypatch.setattr(model_mod.settings, "OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "sk-test-not-real")
    m = configured_model()
    assert isinstance(m, model_mod.OpenRouterMappingModel) and m.name == model_mod.settings.OPENROUTER_MAPPING_MODEL
    monkeypatch.setattr(model_mod.settings, "OPENROUTER_API_KEY", "")
    assert isinstance(configured_model(), model_mod.AnthropicMappingModel)
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "")
    assert configured_model() is None


def test_openrouter_without_a_key_is_rules_only(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "openrouter")
    monkeypatch.setattr(model_mod.settings, "OPENROUTER_API_KEY", "")
    assert configured_model() is None


def test_openrouter_mapping_model_is_its_own_setting_not_the_ocr_model(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "openrouter")
    monkeypatch.setattr(model_mod.settings, "OPENROUTER_MAPPING_MODEL", "vendor/strong-model")
    assert configured_model().name == "vendor/strong-model" != model_mod.settings.OPENROUTER_MODEL


def test_openrouter_adapter_asks_for_the_answer_schema_on_its_model(monkeypatch):
    from app.services import llm

    seen = {}

    def fake(messages, budget, **options):
        seen.update(options)
        return '{"category": {"kind": "fixed", "category_id": 3, "confidence": 0.7}}'

    monkeypatch.setattr(llm, "call_openrouter_within", fake)
    model_mod.OpenRouterMappingModel(model="vendor/strong-model").propose("s", "u")
    assert seen["model"] == "vendor/strong-model"
    fmt = seen["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["schema"] == model_mod.answer_schema()
    assert seen["extra_body"] == {"provider": {"require_parameters": True}}
    # A temperature would make OpenRouter route to no endpoint for Opus 5.5.
    assert seen["temperature"] is None


def test_a_call_without_temperature_leaves_it_out_of_the_request(monkeypatch):
    from types import SimpleNamespace

    from app.services import llm

    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        msg = SimpleNamespace(content='{"ok": true}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    llm.call_openrouter_within([{"role": "user", "content": "x"}], 5, model="vendor/m", temperature=None)
    llm.call_openrouter_within([{"role": "user", "content": "x"}], 5)
    assert "temperature" not in sent[0]
    assert sent[1]["temperature"] == 0.1


def test_answer_schema_is_closed_and_has_no_titles_or_defaults():
    import json

    schema = model_mod.answer_schema()
    text = json.dumps(schema)
    assert '"title"' not in text and '"default"' not in text

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    found = list(objects(schema))
    assert found and all(o["additionalProperties"] is False for o in found)
    assert set(schema["properties"]) == set(ModelAnswer.model_fields)


def test_a_cut_off_openrouter_answer_is_a_model_error(monkeypatch):
    from types import SimpleNamespace

    from app.services import llm

    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"categ'))], usage=None)
    monkeypatch.setattr(llm.client.chat.completions, "create", lambda **kwargs: response)
    with pytest.raises(ModelError):
        model_mod.OpenRouterMappingModel(model="vendor/strong-model", budget_s=5).propose("s", "u")


def test_openrouter_adapter_parses_json_and_refuses_bad_shapes(monkeypatch):
    from app.services import llm

    monkeypatch.setattr(llm, "call_openrouter_within", lambda messages, budget, **options: '```json\n{"category": {"kind": "fixed", "category_id": 3, "confidence": 0.7}}\n```')
    answer = model_mod.OpenRouterMappingModel(model="test").propose("s", "u")
    assert answer.category.category_id == 3
    monkeypatch.setattr(llm, "call_openrouter_within", lambda messages, budget, **options: '{"category": {"kind": "weird"}}')
    with pytest.raises(ModelError):
        model_mod.OpenRouterMappingModel(model="test").propose("s", "u")
    monkeypatch.setattr(llm, "call_openrouter_within", lambda messages, budget, **options: (_ for _ in ()).throw(llm.LLMError("busy")))
    with pytest.raises(ModelError):
        model_mod.OpenRouterMappingModel(model="test").propose("s", "u")


def test_null_parts_in_a_json_mode_answer_read_as_not_given():
    # JSON-mode models (OpenRouter/Gemini) fill unused parts with null; the
    # answer must still validate instead of being thrown away as a bad shape.
    raw = {
        "site_column": "Plant",
        "category": {"kind": "fixed", "category_id": 1, "column": None, "values": None, "confidence": 0.95},
        "period": {"kind": "column", "column": "Date", "date": None, "year_type": None, "year": None, "columns": None, "confidence": 1.0},
        "fields": [{"key": "Fuel Type", "column": "Fuel", "confidence": None}],
        "activities": [{"sheet_text": "HSD", "category_id": 1, "factor_name": "Diesel", "confidence": 0.95}],
        "notes": None,
    }
    answer = ModelAnswer.model_validate(raw)
    assert answer.category.values == [] and answer.category.category_id == 1
    assert answer.period.columns == [] and answer.period.column == "Date"
    assert answer.fields[0].confidence == 0.0 and answer.notes == ""
    # a required part that is null still fails
    with pytest.raises(Exception):
        ModelAnswer.model_validate({"category": {"kind": None, "category_id": 1}})


# ---------------------------------------------------------------------------
# Review findings, part 2/2 (2026-10-14)
# ---------------------------------------------------------------------------


def _draft(name, index=0):
    from app.services.mapping.layers import Draft, rules_layer

    wb = read_workbook(XI1 / name)
    d = Draft(sheet=wb.sheets[index], ctx=ctx())
    rules_layer(d, lookup())
    return d


def test_overlong_model_strings_never_break_the_proposal():
    long = "x" * 5000
    model = FakeModel(
        {
            "site_column": long,
            "period": {"kind": "column", "column": long},
            "fields": [{"key": long, "column": "Unit"}, {"key": "supplier", "column": long}],
            "activities": [{"sheet_text": "Petrol", "category_id": 5, "factor_name": long, "confidence": 0.9}],
        }
    )
    p, s = run("07-months-as-columns.xlsx", model)
    MappingProposal.model_validate(p.model_dump())
    assert all(len(t) <= 300 for t in s.not_in_context)


def test_an_answer_the_contract_refuses_leaves_the_rules_result(monkeypatch):
    from app.services.mapping import llm

    def corrupt(draft, answer, activities):
        draft.category = llm.Part({"kind": "fixed", "id": "not a number"}, 0.9, "llm")

    monkeypatch.setattr(llm, "apply_answer", corrupt)
    with_model, s1 = run("07-months-as-columns.xlsx", FakeModel({}))
    _, s2 = run("07-months-as-columns.xlsx", None)
    assert with_model.model.calls == 1
    assert s1.model_dump(exclude={"source"}) == s2.model_dump(exclude={"source"})


def test_an_adapter_bug_is_not_a_failed_import():
    class Broken(FakeModel):
        def propose(self, system, user):
            raise KeyError("boom")

    p, s = run("07-months-as-columns.xlsx", Broken())
    _, rules = run("07-months-as-columns.xlsx", None)
    assert p.model.calls == 1 and s.model_dump(exclude={"source"}) == rules.model_dump(exclude={"source"})
    assert p.model.error == "KeyError"


def test_a_failed_call_names_the_provider_status_not_its_message():
    class Status402(Exception):
        status_code = 402

    class NoCredit(FakeModel):
        def propose(self, system, user):
            try:
                raise Status402("Insufficient credits: secret request text")
            except Status402 as e:
                raise ModelError("The mapping model gave no usable answer.") from e

    p, _ = run("07-months-as-columns.xlsx", NoCredit())
    assert p.model.calls == 1 and p.model.error == "http_402"
    assert "secret" not in p.model_dump_json()
    MappingProposal.model_validate(p.model_dump())


def test_a_good_answer_and_no_call_carry_no_error():
    p, _ = run("12-large-ledger.csv", FakeModel())
    assert p.model.error is None and "error" not in p.model_dump(exclude_none=True)["model"]


def test_a_prompt_over_budget_is_not_sliced_and_makes_no_call(monkeypatch):
    from app.services.mapping import prompt

    monkeypatch.setattr(prompt, "PROMPT_CHAR_BUDGET", 500)
    assert prompt.build_prompt(_draft("07-months-as-columns.xlsx"), lookup()) is None
    model = FakeModel({"category": {"kind": "fixed", "category_id": 5}})
    p, _ = run("07-months-as-columns.xlsx", model)
    assert model.calls == [] and p.model.calls == 0


def test_long_resolved_and_needed_strings_are_clipped_in_the_prompt():
    import json

    from app.services.mapping.layers import Part
    from app.services.mapping.prompt import CELL_MAX_CHARS, build_prompt

    d = _draft("07-months-as-columns.xlsx")
    d.activities["y" * 1000] = Part(None, 0.0, "rules")
    d.activities["z" * 1000] = Part("Diesel", 0.9, "rules")
    body = json.loads(build_prompt(d, lookup())[1])
    assert all(len(t) <= CELL_MAX_CHARS + 1 for t in body["NEEDED"]["activities"])
    assert all(len(t) <= CELL_MAX_CHARS + 1 for t in body["ALREADY_RESOLVED"]["activities"])


def test_invented_sheet_wordings_are_ignored():
    model = FakeModel({"activities": [{"sheet_text": "Not in the sheet", "category_id": 3, "factor_name": "Diesel", "confidence": 0.9}]})
    _, s = run("07-months-as-columns.xlsx", model)
    assert "Not in the sheet" not in {a.sheet_text for a in s.activities}


def test_column_category_values_come_from_the_column_not_the_model():
    model = FakeModel({"category": {"kind": "column", "column": "Unit", "values": [{"text": "kWh", "category_id": 5, "confidence": 0.9}, {"text": "Invented", "category_id": 3, "confidence": 0.9}]}})
    _, s = run("07-months-as-columns.xlsx", model)
    texts = [v.text for v in s.category.values]
    assert "Invented" not in texts and set(texts) == {"litre", "kWh"}
    assert {v.text: v.id for v in s.category.values}["litre"] is None


def test_an_activity_in_another_category_than_its_row_is_ignored():
    model = FakeModel(
        {
            "category": {"kind": "column", "column": "Unit", "values": [{"text": "litre", "category_id": 3, "confidence": 0.9}, {"text": "kWh", "category_id": 5, "confidence": 0.9}]},
            "activities": [{"sheet_text": "Diesel", "category_id": 5, "factor_name": "Grid electricity", "confidence": 0.9}],
        }
    )
    _, s = run("07-months-as-columns.xlsx", model)
    assert {a.sheet_text: a.factor_name for a in s.activities}.get("Diesel") != "Grid electricity"


def test_a_model_date_that_is_not_a_real_date_is_refused():
    from app.services.mapping.llm import apply_answer

    for bad in ("2025-02-30", "31/01/2025", "2025-13-01"):
        d = _draft("07-months-as-columns.xlsx")
        d.period = None
        apply_answer(d, ModelAnswer.model_validate({"period": {"kind": "monthly_fixed", "date": bad, "confidence": 0.9}}), lookup())
        assert d.period is None
        apply_answer(d, ModelAnswer.model_validate({"period": {"kind": "months_as_columns", "columns": [{"column": "Jan-25", "date": bad}]}}), lookup())
        assert d.period is None
    apply_answer(d, ModelAnswer.model_validate({"period": {"kind": "monthly_fixed", "date": "2025-02-28", "confidence": 0.9}}), lookup())
    assert d.period.value == {"kind": "monthly_fixed", "date": "2025-02-28"}


class _FakeMessages:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def parse(self, **kwargs):
        if self.error:
            raise self.error
        return self.result


def _anthropic(result=None, error=None):
    m = model_mod.AnthropicMappingModel(api_key="sk-test-not-real", model="test")
    m._client = type("C", (), {"messages": _FakeMessages(result, error)})()
    return m


def test_anthropic_adapter_turns_truncated_or_unparsable_answers_into_model_errors():
    from types import SimpleNamespace

    from pydantic import ValidationError

    try:
        ModelAnswer.model_validate({"category": {"kind": "weird"}})
    except ValidationError as e:
        bad = e
    with pytest.raises(ModelError):
        _anthropic(error=bad).propose("s", "u")
    with pytest.raises(ModelError):
        _anthropic(error=ValueError("not json")).propose("s", "u")
    truncated = SimpleNamespace(stop_reason="max_tokens", parsed_output=ModelAnswer(), usage=None)
    with pytest.raises(ModelError):
        _anthropic(truncated).propose("s", "u")
    ok = SimpleNamespace(stop_reason="end_turn", parsed_output=ModelAnswer(notes="ok"), usage=None)
    assert _anthropic(ok).propose("s", "u").notes == "ok"


def test_configured_model_is_built_once_per_settings(monkeypatch):
    monkeypatch.setattr(model_mod.settings, "MAPPING_MODEL_PROVIDER", "anthropic")
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "sk-test-not-real")
    first = configured_model()
    assert configured_model() is first
    monkeypatch.setattr(model_mod.settings, "ANTHROPIC_API_KEY", "sk-test-other")
    assert configured_model() is not first
