"""Smart Excel import contracts (XI0): the python twin of ESG-lite
``src/imports/mapping.ts`` (``docs/excel-import/01-contracts.md``).

A ``MappingProposal`` is what the mapping engine (XI2) works out from a
workbook for ONE site (imports are site-wise, Shyam 2026-10-14) and what the
user confirms on screen. Every ``site_id``, ``category_id``, field key and
``factor_name`` must exist in the caller's import context; the engine drops
anything else and lists it under ``not_in_context``. That rule is enforced by
code, not by these models. Unknown keys are ignored (ESG-lite strips them).

The JSON fixtures under ``ci/fixtures/xi0`` are shared byte for byte with
ESG-lite; both repos' tests parse them.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ESG-lite src/imports/limits.ts (MAPPING_*): one source of truth, mirrored here.
MAPPING_SHEETS_PER_WORKBOOK = 20
MAPPING_SAMPLE_ROWS_PER_SHEET = 15
MAPPING_DISTINCT_VALUES_PER_COLUMN = 200
MAPPING_PROMPT_TOKENS_PER_SHEET = 8000
MAPPING_MODEL_CALLS_PER_SHEET = 1
MAPPING_PROPOSE_PER_WINDOW = 10
MAPPING_PROPOSE_WINDOW_MINUTES = 10
MAPPING_CONFIRM_BELOW = 0.8
MAPPING_TEXT_MAX_CHARS = 300

# Row issue codes this service may raise locally (ESG-lite src/imports/errors.ts).
SITE_NOT_ALLOWED = "SITE_NOT_ALLOWED"          # row names a site other than the chosen one
CATEGORY_NOT_ASSIGNED = "CATEGORY_NOT_ASSIGNED"
NOT_IN_CONTEXT = "NOT_IN_CONTEXT"
LOW_CONFIDENCE_MATCH = "LOW_CONFIDENCE_MATCH"
PIVOT_EMPTY_CELL = "PIVOT_EMPTY_CELL"
AI_IMPORT_DISABLED_FOR_CLIENT = "AI_IMPORT_DISABLED_FOR_CLIENT"

MappingSource = Literal["profile", "rules", "llm"]
SheetSource = Literal["profile", "rules", "llm", "mixed"]
SheetLayout = Literal["rows", "months_as_columns"]
YearType = Literal["CY", "FY"]

PositiveInt = Annotated[int, Field(gt=0)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Text = Annotated[str, Field(max_length=MAPPING_TEXT_MAX_CHARS)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ImportRowMeta(_Strict):
    """Provenance kept on ``ImportRow.meta`` (never read by the engine)."""

    source: MappingSource
    confidence: Confidence


class FixedCategory(_Strict):
    kind: Literal["fixed"]
    id: PositiveInt
    label: Text
    confidence: Confidence
    source: MappingSource


class CategoryValue(_Strict):
    text: Text
    id: Optional[PositiveInt] = None
    confidence: Confidence
    source: MappingSource


class ColumnCategory(_Strict):
    kind: Literal["column"]
    column: Text
    values: Annotated[list[CategoryValue], Field(max_length=MAPPING_DISTINCT_VALUES_PER_COLUMN)]


ResolvedCategory = Annotated[Union[FixedCategory, ColumnCategory], Field(discriminator="kind")]


class ColumnPeriod(_Strict):
    kind: Literal["column"]
    column: Text


class MonthlyFixedPeriod(_Strict):
    kind: Literal["monthly_fixed"]
    date: IsoDate


class YearlyPeriod(_Strict):
    kind: Literal["yearly"]
    year_type: YearType
    year: Annotated[int, Field(ge=2000, le=2100)]


class MonthColumn(_Strict):
    column: Text
    date: IsoDate


class MonthsAsColumnsPeriod(_Strict):
    kind: Literal["months_as_columns"]
    columns: Annotated[list[MonthColumn], Field(min_length=1, max_length=120)]


SheetPeriod = Annotated[Union[ColumnPeriod, MonthlyFixedPeriod, YearlyPeriod, MonthsAsColumnsPeriod], Field(discriminator="kind")]


class FieldMapping(_Strict):
    key: Annotated[str, Field(max_length=100)]
    label: Text
    required: bool
    column: Optional[Text] = None
    confidence: Confidence
    source: MappingSource
    skipped: bool = False


class ProposedActivityMatch(_Strict):
    sheet_text: Text
    category_id: PositiveInt
    factor_name: Optional[Text] = None
    confidence: Confidence
    source: MappingSource
    save_mapping: bool = False


class SiteColumnValue(_Strict):
    text: Text
    matches_site: bool


class SiteColumn(_Strict):
    """Informational: rows whose text does not match the chosen site get SITE_NOT_ALLOWED."""

    column: Text
    values: Annotated[list[SiteColumnValue], Field(max_length=MAPPING_DISTINCT_VALUES_PER_COLUMN)]


class SheetProposal(_Strict):
    sheet_name: Text
    fingerprint: Fingerprint
    header_row: PositiveInt
    data_rows: Annotated[int, Field(ge=0)]
    layout: SheetLayout
    site_id: PositiveInt
    site_column: Optional[SiteColumn] = None
    category: ResolvedCategory
    period: SheetPeriod
    fields: list[FieldMapping]
    activities: list[ProposedActivityMatch]
    not_in_context: list[Text] = Field(default_factory=list)
    confidence: Confidence
    source: SheetSource
    excluded: bool = False


class ModelTelemetry(_Strict):
    name: Annotated[str, Field(max_length=100)]
    calls: Annotated[int, Field(ge=0)]
    cached_profile_hits: Annotated[int, Field(ge=0)]


class MappingProposal(_Strict):
    document_id: PositiveInt
    site_id: PositiveInt
    sheets: Annotated[list[SheetProposal], Field(max_length=MAPPING_SHEETS_PER_WORKBOOK)]
    model: Optional[ModelTelemetry] = None

    @model_validator(mode="after")
    def _one_site(self) -> "MappingProposal":
        if any(s.site_id != self.site_id for s in self.sheets):
            raise ValueError("every sheet must be for the proposal's site")
        return self


class MappingProfile(_Strict):
    """``GET /imports/mapping-profiles`` row (XI3)."""

    id: str
    company_id: PositiveInt
    site_id: PositiveInt
    category_id: Optional[PositiveInt] = None
    fingerprint: Fingerprint
    sheet_proposal: SheetProposal
    confirmed_by: Optional[PositiveInt] = None
    confirmed_at: Optional[str] = None
    uses: Annotated[int, Field(ge=0)]
    created_at: str
    updated_at: str

    @field_validator("id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v):
            raise ValueError("uuid expected")
        return v


def needs_confirmation(confidence: float) -> bool:
    """Whether a part needs the user's eyes before Continue (decision 4)."""
    return confidence < MAPPING_CONFIRM_BELOW
