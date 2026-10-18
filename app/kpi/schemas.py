"""KPI-12 · request and answer models for /v1/kpi/* (ESG-lite docs/kpi/01-contracts §3, layer file app/kpi/CLAUDE.md).

Unknown keys are ignored, so ESG-lite may send more than is read here.
Everything returned is a candidate: ESG-lite stores nothing until a person
confirms it in the entry card.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_validator

MAX_KPIS = 200
MAX_CATALOGUE = 5000
MAX_MAPPING_SUGGESTIONS = 5
MAX_CANDIDATES_PER_KPI = 5
QUOTE_MAX_CHARS = 300
WHY_MAX_CHARS = 200
# Below this the FE shows a suggestion as "Check this" only (layer rule 4).
CHECK_THIS_BELOW = 0.6


# ---------------------------------------------------------------------------
# POST /v1/kpi/suggest-values
# ---------------------------------------------------------------------------

class KpiAsked(BaseModel):
    """One selected KPI the caller may fill: the only codes a candidate may name."""

    code: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=1000)
    unit: Optional[str] = Field(default=None, max_length=50)
    allowed_units: list[str] = Field(default_factory=list, max_length=50)
    # 01-contracts §2 kpi_definition.breakdowns, e.g. {"by": ["source"], "values": ["groundwater", "surface"]},
    # or a plain list of keys.
    breakdowns: Optional[Any] = None

    @field_validator("code")
    @classmethod
    def strip_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("code must not be blank")
        return v

    @field_validator("allowed_units")
    @classmethod
    def clean_units(cls, v: list[str]) -> list[str]:
        return [u.strip() for u in v if isinstance(u, str) and u.strip()]

    def breakdown_keys(self) -> list[str]:
        b = self.breakdowns
        if isinstance(b, dict):
            b = b.get("values")
        if isinstance(b, list):
            return [str(x).strip() for x in b if isinstance(x, (str, int)) and str(x).strip()]
        return []


class PeriodHint(BaseModel):
    period_start: Optional[dt.date] = None
    period_end: Optional[dt.date] = None
    frequency: Optional[str] = Field(default=None, max_length=20)


class SuggestValuesRequest(BaseModel):
    company_id: int = Field(gt=0)
    document_id: int = Field(gt=0)
    kpis: list[KpiAsked] = Field(min_length=1, max_length=MAX_KPIS)
    # The period the caller is filling: {period_start, period_end, frequency} or free text ("FY 2025-26").
    period_hint: Optional[Union[PeriodHint, str]] = None
    # Optional: when set, the document must have been uploaded by this user.
    user_id: Optional[int] = Field(default=None, gt=0)

    @field_validator("kpis")
    @classmethod
    def unique_codes(cls, v: list[KpiAsked]) -> list[KpiAsked]:
        if len({k.code for k in v}) != len(v):
            raise ValueError("kpi codes must be unique")
        return v

    @field_validator("period_hint", mode="before")
    @classmethod
    def short_hint(cls, v):
        if isinstance(v, str):
            v = v.strip()[:100] or None
        return v


class ValueCandidate(BaseModel):
    kpi_code: str
    value: Union[float, str]
    unit: Optional[str]
    breakdown_key: Optional[str] = None
    period_start: Optional[dt.date] = None
    period_end: Optional[dt.date] = None
    site_hint: Optional[str] = None
    source_page: int = Field(ge=1)
    quote: str
    confidence: float = Field(ge=0, le=1)
    why: str


class SuggestValuesResponse(BaseModel):
    document_id: int
    document_name: str
    pages: int
    candidates: list[ValueCandidate]


# ---------------------------------------------------------------------------
# POST /v1/kpi/suggest-mapping
# ---------------------------------------------------------------------------

class CatalogueDisclosure(BaseModel):
    framework: str = Field(min_length=1, max_length=40)
    code: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=500)
    section: Optional[str] = Field(default=None, max_length=200)


class SuggestMappingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    question: Optional[str] = Field(default=None, max_length=1000)
    unit: Optional[str] = Field(default=None, max_length=50)
    topic: Optional[str] = Field(default=None, max_length=50)
    catalogue: list[CatalogueDisclosure] = Field(min_length=1, max_length=MAX_CATALOGUE)
    company_id: Optional[int] = Field(default=None, gt=0)  # logs only

    @field_validator("catalogue")
    @classmethod
    def unique_disclosures(cls, v: list[CatalogueDisclosure]) -> list[CatalogueDisclosure]:
        if len({(d.framework, d.code) for d in v}) != len(v):
            raise ValueError("catalogue (framework, code) pairs must be unique")
        return v


class MappingSuggestion(BaseModel):
    framework: str
    code: str
    title: str
    section: Optional[str] = None
    why: str
    confidence: float = Field(ge=0, le=1)


class SuggestMappingResponse(BaseModel):
    suggestions: list[MappingSuggestion] = Field(max_length=MAX_MAPPING_SUGGESTIONS)
