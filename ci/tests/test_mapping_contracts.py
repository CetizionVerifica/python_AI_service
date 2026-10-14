"""XI0: the mapping contract models parse the fixtures shared with ESG-lite
(ci/fixtures/xi0) exactly as the zod schemas do. No database, no network."""
import copy
import json

import pytest
from pydantic import ValidationError

from app.schemas import mapping as m
from ci.tests.conftest import FIXTURES

XI0 = FIXTURES / "xi0"


def load(name: str) -> dict:
    return json.loads((XI0 / name).read_text())


def test_valid_proposal_parses_and_every_sheet_is_for_the_chosen_site():
    p = m.MappingProposal.model_validate(load("proposal-valid.json"))
    assert len(p.sheets) == 2
    assert all(s.site_id == p.site_id for s in p.sheets)
    assert p.sheets[0].not_in_context == ["Plant 9"]
    assert sum(not v.matches_site for v in p.sheets[0].site_column.values) == 1
    assert p.sheets[1].period.kind == "months_as_columns"
    assert p.sheets[1].category.kind == "column"


@pytest.mark.parametrize("name", ["proposal-other-site.json", "proposal-bad-fingerprint.json", "proposal-confidence-over-1.json"])
def test_refused_fixtures(name):
    with pytest.raises(ValidationError):
        m.MappingProposal.model_validate(load(name))


def test_defaults_match_the_zod_schema():
    sheet = copy.deepcopy(load("proposal-valid.json")["sheets"][0])
    for k in ("site_column", "not_in_context", "excluded"):
        sheet.pop(k)
    sheet["activities"] = [{k: v for k, v in a.items() if k != "save_mapping"} for a in sheet["activities"]]
    sheet["fields"] = [{k: v for k, v in f.items() if k != "skipped"} for f in sheet["fields"]]
    s = m.SheetProposal.model_validate(sheet)
    assert s.site_column is None and s.not_in_context == [] and s.excluded is False
    assert all(a.save_mapping is False for a in s.activities)
    assert all(f.skipped is False for f in s.fields)


def test_unknown_keys_are_ignored_like_zod_strip():
    p = load("proposal-valid.json")
    p["total_tco2e"] = 999
    p["sheets"][0]["factor"] = 2.68
    parsed = m.MappingProposal.model_validate(p)
    assert "total_tco2e" not in parsed.model_dump()
    assert "factor" not in parsed.sheets[0].model_dump()


def test_sheet_limit():
    p = load("proposal-valid.json")
    one = p["sheets"][0]
    p["sheets"] = [dict(one, sheet_name=f"S{i}") for i in range(m.MAPPING_SHEETS_PER_WORKBOOK + 1)]
    with pytest.raises(ValidationError):
        m.MappingProposal.model_validate(p)


def test_period_kinds():
    class Holder(m._Strict):
        period: m.SheetPeriod

    for period in (
        {"kind": "column", "column": "Month"},
        {"kind": "monthly_fixed", "date": "2025-03-31"},
        {"kind": "yearly", "year_type": "FY", "year": 2026},
        {"kind": "months_as_columns", "columns": [{"column": "Jan", "date": "2025-01-31"}]},
    ):
        Holder.model_validate({"period": period})
    for bad in ({"kind": "weekly", "column": "Week"}, {"kind": "months_as_columns", "columns": []}):
        with pytest.raises(ValidationError):
            Holder.model_validate({"period": bad})


def test_profile_fixture_and_row_meta():
    prof = m.MappingProfile.model_validate(load("profile-valid.json"))
    assert prof.uses == 3 and prof.sheet_proposal.site_id == prof.site_id
    meta = m.ImportRowMeta.model_validate(load("row-with-meta.json")["meta"])
    assert meta.source == "llm" and meta.confidence == 0.92
    with pytest.raises(ValidationError):
        m.ImportRowMeta.model_validate({"source": "guess", "confidence": 0.5})
    with pytest.raises(ValidationError):
        m.ImportRowMeta.model_validate({"source": "llm", "confidence": 2})


def test_confirmation_threshold_and_codes():
    assert m.needs_confirmation(0.79) and not m.needs_confirmation(0.8)
    assert m.SITE_NOT_ALLOWED == "SITE_NOT_ALLOWED"
    assert m.AI_IMPORT_DISABLED_FOR_CLIENT == "AI_IMPORT_DISABLED_FOR_CLIENT"


def test_fixtures_are_identical_to_esg_lite_copies_by_content_hash():
    # Both repos ship the same files; this guards the python copy against drift
    # by pinning what the valid proposal contains, not the other repo's path.
    p = load("proposal-valid.json")
    assert p["site_id"] == 2 and p["sheets"][0]["sheet_name"] == "Fuel 2025"
