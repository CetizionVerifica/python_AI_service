"""XI0: the mapping contract models parse the fixtures shared with ESG-lite
(ci/fixtures/xi0) exactly as the zod schemas do. No database, no network."""
import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.schemas import mapping as m
from ci.tests.conftest import FIXTURES

XI0 = FIXTURES / "xi0"


def load(name: str) -> dict:
    return json.loads((XI0 / name).read_text())


# sha256 of each shared fixture. ESG-lite ci/unit/xi0-mapping.test.cjs pins the
# same values, so the two copies cannot drift apart silently. Update both when
# a fixture changes on purpose.
FIXTURE_SHA256 = {
    "profile-valid.json": "d46cff40ae067d8345351ae493d9bc480662ba8d2a177b9a34daf2ff8a0e4b37",
    "proposal-bad-fingerprint.json": "4d5cbbc0f82642ab5945a0d4532011e487be08f3288bcc22f6f996dc97682805",
    "proposal-confidence-over-1.json": "b156638fe1d1af27ff29a4e97f1951b66cff6b7ea3d0c5d84f11e2d568fc93ff",
    "proposal-other-site.json": "55986fef5e9685393ddf2fbcfdb36a55e4d3b24025efb74ce2063392a9a6f85c",
    "proposal-valid.json": "0635d17429adcd9276527cbc83c9be1beadf97ef4b2322e336a11f16e2b54a95",
    "row-with-meta.json": "50de2a7136e885fd8e38cda815d07d43d2a1b32e244e9d52cff1f9f2c7d47c48",
}


def test_shared_fixtures_are_byte_identical_to_the_esg_lite_copies():
    present = sorted(p.name for p in XI0.glob("*.json"))
    assert present == sorted(FIXTURE_SHA256)
    for name, sha in FIXTURE_SHA256.items():
        actual = hashlib.sha256((XI0 / name).read_bytes()).hexdigest()
        assert actual == sha, f"{name} changed; update the pin here and in ESG-lite"


def test_profile_id_accepts_either_uuid_case():
    profile = load("profile-valid.json")
    upper = dict(profile, id=profile["id"].upper())
    assert m.MappingProfile.model_validate(upper).id == profile["id"].upper()
    with pytest.raises(ValidationError):
        m.MappingProfile.model_validate(dict(profile, id="not-a-uuid"))


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
