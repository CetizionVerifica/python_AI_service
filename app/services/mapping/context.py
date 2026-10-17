"""The caller's import context (IM3) as the mapping engine sees it: one site,
that site's categories with their forms and units, and the activity names
each category accepts. Everything is read through ESG-lite with the caller's
own token (``PlanClient``), so it is exactly what the caller may import into.
Nothing outside this view may appear in a proposal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.services.excel_plan_client import EsgLiteError, PlanClient

logger = logging.getLogger(__name__)

# The keys every category has, before its form's extra fields.
VALUE_KEY = "activity_value"
UNIT_KEY = "activity_unit"
ACTIVITY_KEY = "activity_name"
ACTIVITY_SEARCH_LIMIT = 50


@dataclass
class FieldSpec:
    key: str
    label: str
    required: bool
    type: str = "text"
    unit: Optional[str] = None
    options: list[str] = field(default_factory=list)


@dataclass
class CategoryView:
    category_id: int
    name: str
    scope: Optional[str]
    period_modes: list[str]
    calc_mode: Optional[str]
    fields: list[FieldSpec]  # the form's own fields (extra_*, distance, …), not value/unit
    units: list[str]
    activity_count: int
    has_form: bool

    def field_specs(self) -> list[FieldSpec]:
        """Value and unit first (unless the form has no value field), then the form's fields."""
        base = [FieldSpec(VALUE_KEY, "Quantity", required=self.calc_mode in (None, "single", "per_method", "per_unit"), type="number"), FieldSpec(UNIT_KEY, "Unit", required=True)]
        return base + list(self.fields)


@dataclass
class ContextView:
    company_id: int
    company_name: str
    role: str
    site_id: int
    site_name: str
    categories: list[CategoryView]
    other_site_names: list[str]  # the caller's other sites, so a site column can be recognised
    fy_start_month: int = 4

    def category(self, category_id: Optional[int]) -> Optional[CategoryView]:
        return next((c for c in self.categories if c.category_id == category_id), None)

    def category_named(self, name: str) -> Optional[CategoryView]:
        return next((c for c in self.categories if c.name == name), None)

    @property
    def category_names(self) -> list[str]:
        return [c.name for c in self.categories]


def context_view(context: dict, site_id: int) -> ContextView:
    """The IM3 context narrowed to one site. ``ValueError`` when the site is not in it."""
    sites = context.get("sites") or []
    site = next((s for s in sites if int(s.get("site_id") or 0) == int(site_id)), None)
    if site is None:
        raise ValueError("site not in context")
    cats: list[CategoryView] = []
    for c in site.get("categories") or []:
        form = c.get("form") or None
        fields = [
            FieldSpec(
                key=str(f.get("key")),
                label=str(f.get("label") or f.get("key")),
                required=bool(f.get("required")),
                type=str(f.get("type") or "text"),
                unit=f.get("unit"),
                options=list(f.get("options") or []),
            )
            for f in (form or {}).get("fields") or []
            if str(f.get("key")) not in (VALUE_KEY, UNIT_KEY)
        ]
        cats.append(
            CategoryView(
                category_id=int(c["category_id"]),
                name=str(c.get("name") or ""),
                scope=c.get("scope"),
                period_modes=list(c.get("period_modes") or ["monthly", "yearly"]),
                calc_mode=(form or {}).get("calc_mode"),
                fields=fields,
                units=[str(u) for u in (c.get("units") or [])],
                activity_count=int(c.get("activity_count") or 0),
                has_form=form is not None,
            )
        )
    company = context.get("company") or {}
    return ContextView(
        company_id=int(company.get("company_id") or 0),
        company_name=str(company.get("name") or ""),
        role=str(context.get("role") or ""),
        site_id=int(site["site_id"]),
        site_name=str(site.get("name") or ""),
        categories=cats,
        other_site_names=[str(s.get("name") or "") for s in sites if int(s.get("site_id") or 0) != int(site_id)],
        fy_start_month=int(context.get("fy_start_month") or 4),
    )


class ActivityLookup:
    """Activity names a category accepts (factor names and P23 mappings), read
    through ESG-lite per category and per sheet wording, cached per request."""

    def __init__(self, client: Optional[PlanClient], site_id: int, company_id: Optional[int] = None):
        self._client = client
        self._site_id = int(site_id)
        self._company_id = company_id
        self._all: dict[int, list[dict]] = {}
        self._queries: dict[tuple[int, str], list[dict]] = {}
        self.calls = 0

    def options(self, category_id: int, q: str = "") -> list[dict]:
        """``ActivityOption`` rows for the category; with ``q`` the ones containing it."""
        if self._client is None:
            return []
        key = (int(category_id), q.strip().lower())
        if not key[1] and key[0] in self._all:
            return self._all[key[0]]
        if key in self._queries:
            return self._queries[key]
        try:
            rows = self._client.search_activities(self._site_id, int(category_id), q=q, limit=ACTIVITY_SEARCH_LIMIT, company_id=self._company_id)
        except EsgLiteError as e:
            # A category the caller may not use, or a factor table that is not there: no names, not a crash.
            logger.info(f"activities for category {category_id} not read ({e.code})")
            rows = []
        self.calls += 1
        if not key[1]:
            self._all[key[0]] = rows
        else:
            self._queries[key] = rows
        return rows

    def names(self, category_id: int, q: str = "") -> list[str]:
        return [str(o.get("name") or "") for o in self.options(category_id, q) if o.get("name")]

    def is_complete(self, category: CategoryView) -> bool:
        """Whether the unfiltered list holds every name the category has."""
        return category.activity_count <= ACTIVITY_SEARCH_LIMIT


class StaticActivityLookup(ActivityLookup):
    """A lookup over fixed names per category (tests and the profile re-check)."""

    def __init__(self, names_by_category: dict[int, list[str]]):
        super().__init__(None, 0)
        self._names = {int(k): list(v) for k, v in names_by_category.items()}

    def options(self, category_id: int, q: str = "") -> list[dict]:
        needle = q.strip().lower()
        rows = [{"name": n, "kind": "factor", "units": [], "latest_year": None} for n in self._names.get(int(category_id), [])]
        if needle:
            rows = [r for r in rows if needle in r["name"].lower()]
        return rows[:ACTIVITY_SEARCH_LIMIT]
