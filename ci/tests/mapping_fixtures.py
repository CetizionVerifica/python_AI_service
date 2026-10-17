"""A fake import context and activity names for the XI2 tests: company 1's
site 2 with four categories, and company 2's site 7 that must never appear."""
from __future__ import annotations

from app.services.mapping.context import StaticActivityLookup, context_view

CONTEXT = {
    "company": {"company_id": 1, "name": "ABC Cables", "mcp_enabled": False},
    "role": "Manager",
    "fy_start_month": 4,
    "sites": [
        {
            "site_id": 2,
            "name": "Plant 2",
            "categories": [
                {
                    "category_id": 3,
                    "name": "Stationary combustion",
                    "scope": "Scope 1",
                    "period_modes": ["monthly", "yearly"],
                    "form": {
                        "calc_mode": "single",
                        "fields": [
                            {"key": "activity_value", "label": "Quantity", "type": "number", "required": True},
                            {"key": "activity_unit", "label": "Unit", "type": "text", "required": True},
                            {"key": "supplier", "label": "Supplier", "type": "text", "required": False},
                        ],
                    },
                    "units": ["litre", "kg", "SCM"],
                    "activity_count": 5,
                    "readiness": {"form": True, "factor_years": [2024, 2025]},
                },
                {"category_id": 4, "name": "Mobile combustion", "scope": "Scope 1", "period_modes": ["monthly"], "form": {"calc_mode": "single", "fields": []}, "units": ["litre"], "activity_count": 3, "readiness": {"form": True, "factor_years": [2025]}},
                {"category_id": 5, "name": "Purchased electricity", "scope": "Scope 2", "period_modes": ["monthly", "yearly"], "form": {"calc_mode": "single", "fields": [{"key": "meter", "label": "Meter no", "type": "text", "required": False}]}, "units": ["kWh"], "activity_count": 2, "readiness": {"form": True, "factor_years": [2025]}},
                {"category_id": 6, "name": "Business travel", "scope": "Scope 3", "period_modes": ["monthly"], "form": {"calc_mode": "distance", "fields": [{"key": "distance", "label": "Distance", "type": "number", "required": True, "unit": "km"}, {"key": "passengers", "label": "Passengers", "type": "number", "required": False}]}, "units": ["km"], "activity_count": 2, "readiness": {"form": True, "factor_years": [2025]}},
            ],
        },
        {"site_id": 9, "name": "Plant 9", "categories": []},
    ],
}

ACTIVITY_NAMES = {3: ["Diesel", "HSD", "Furnace oil", "Natural gas", "LPG"], 4: ["Diesel", "Petrol", "CNG"], 5: ["Grid electricity", "Grid"], 6: ["Air travel - domestic", "Rail"]}

# Company 2: a site and names that must never show up for company 1.
OTHER_COMPANY_CONTEXT = {
    "company": {"company_id": 2, "name": "Zed Steel", "mcp_enabled": False},
    "role": "Manager",
    "fy_start_month": 4,
    "sites": [{"site_id": 7, "name": "Zed Works", "categories": [{"category_id": 30, "name": "Process emissions", "scope": "Scope 1", "period_modes": ["monthly"], "form": {"calc_mode": "single", "fields": []}, "units": ["t"], "activity_count": 1, "readiness": {"form": True, "factor_years": [2025]}}]}],
}
OTHER_ACTIVITY_NAMES = {30: ["Zed secret clinker"]}


def ctx(site_id=2):
    return context_view(CONTEXT, site_id)


def lookup():
    return StaticActivityLookup(ACTIVITY_NAMES)
