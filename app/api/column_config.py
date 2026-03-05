"""
Route for LLM-powered column name inference from emission category names.
Used by the auto-generate column config feature.
"""

import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.llm import _call_openrouter, _parse_json_object

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Schemas ---

class DimensionInfo(BaseModel):
    position: int
    sample_values: list[str]


class InferColumnsRequest(BaseModel):
    category_name: str
    dimensions: list[DimensionInfo]
    denominator_unit: str
    existing_columns: list[str] = []


class InferredColumn(BaseModel):
    position: int
    suggested_name: str
    reuse_existing: Optional[str] = None


class InferColumnsResponse(BaseModel):
    columns: list[InferredColumn]
    activity_column_name: str
    suggested_units: list[str]


# --- Multi-group schemas ---

class DimensionGroup(BaseModel):
    dim_count: int
    dimensions: list[DimensionInfo]


class InferAllColumnsRequest(BaseModel):
    category_name: str
    dimension_groups: list[DimensionGroup]
    denominator_unit: str
    existing_columns: list[str] = []


class InferredGroupResult(BaseModel):
    dim_count: int
    columns: list[InferredColumn]
    activity_column_name: str


class InferAllColumnsResponse(BaseModel):
    groups: list[InferredGroupResult]
    suggested_units: list[str]


# --- Endpoints ---

@router.post("/column-config/infer-columns", response_model=InferColumnsResponse)
async def infer_columns(body: InferColumnsRequest):
    """
    Use LLM to infer meaningful column names from emission category dimension values.
    Single dimension group variant (backward compatible).
    """
    try:
        prompt = _build_inference_prompt(body)
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "category_name": body.category_name,
                    "dimensions": [
                        {
                            "position": d.position,
                            "sample_values": d.sample_values[:15],
                        }
                        for d in body.dimensions
                    ],
                    "denominator_unit": body.denominator_unit,
                    "existing_columns": body.existing_columns,
                }),
            },
        ]

        raw = _call_openrouter(messages)
        parsed = _parse_json_object(raw)

        columns = []
        for col in parsed.get("columns", []):
            columns.append(InferredColumn(
                position=col["position"],
                suggested_name=col["suggested_name"],
                reuse_existing=col.get("reuse_existing"),
            ))

        return InferColumnsResponse(
            columns=columns,
            activity_column_name=parsed.get("activity_column_name", "Activity Data"),
            suggested_units=parsed.get("suggested_units", [body.denominator_unit]),
        )

    except Exception as e:
        logger.error(f"Column inference failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM inference failed: {str(e)}")


@router.post("/column-config/infer-all-columns", response_model=InferAllColumnsResponse)
async def infer_all_columns(body: InferAllColumnsRequest):
    """
    Infer column names for ALL dimension groups in a single LLM call.
    Used when a category has emission factors with mixed dimension counts
    (e.g., 2-dim and 3-dim names in the same category).
    """
    try:
        prompt = _build_multi_group_prompt(body)
        user_data = {
            "category_name": body.category_name,
            "denominator_unit": body.denominator_unit,
            "existing_columns": body.existing_columns,
            "dimension_groups": [
                {
                    "dim_count": g.dim_count,
                    "dimensions": [
                        {"position": d.position, "sample_values": d.sample_values[:15]}
                        for d in g.dimensions
                    ],
                }
                for g in body.dimension_groups
            ],
        }
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_data)},
        ]

        raw = _call_openrouter(messages)
        parsed = _parse_json_object(raw)

        groups = []
        for group in parsed.get("groups", []):
            columns = []
            for col in group.get("columns", []):
                columns.append(InferredColumn(
                    position=col["position"],
                    suggested_name=col["suggested_name"],
                    reuse_existing=col.get("reuse_existing"),
                ))
            groups.append(InferredGroupResult(
                dim_count=group["dim_count"],
                columns=columns,
                activity_column_name=group.get("activity_column_name", "Activity Data"),
            ))

        return InferAllColumnsResponse(
            groups=groups,
            suggested_units=parsed.get("suggested_units", [body.denominator_unit]),
        )

    except Exception as e:
        logger.error(f"Multi-group column inference failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM inference failed: {str(e)}")


def _build_inference_prompt(body: InferColumnsRequest) -> str:
    return f"""You are an ESG (Environmental, Social, Governance) data configuration assistant.

Your task: Given emission category dimension values for a GHG reporting category, suggest meaningful column names for a data entry form.

Context:
- The GHG category is: "{body.category_name}"
- The emission factor unit is: "{body.denominator_unit}"
- Emission category names were split by " - " into dimensions. Each dimension represents a dropdown column in the data entry form.
- There are already existing columns in the system: {json.dumps(body.existing_columns)}

Rules:
1. For each dimension, suggest a clear, concise column name (2-4 words max).
2. If an existing column name matches well, set "reuse_existing" to that exact name.
3. For the activity/numeric column, suggest a name based on the unit (e.g., "Weight" for tonnes/kg, "Volume" for litres, "Spent Value" for USD/INR, "Distance" for km, "Energy Consumed" for kWh/MWh).
4. If the unit suggests the data can also be entered in related units, list them in "suggested_units" (e.g., for "ton" suggest ["ton", "kg", "tonnes"]).

Return a JSON object with this exact structure:
{{
  "columns": [
    {{ "position": 0, "suggested_name": "Waste Type", "reuse_existing": "Waste Type" }},
    {{ "position": 1, "suggested_name": "Disposal Method", "reuse_existing": "Disposal Method" }}
  ],
  "activity_column_name": "Weight",
  "suggested_units": ["ton", "kg", "tonnes"]
}}

Return ONLY the JSON object, no other text."""


def _build_multi_group_prompt(body: InferAllColumnsRequest) -> str:
    dim_counts = [g.dim_count for g in body.dimension_groups]
    return f"""You are an ESG (Environmental, Social, Governance) data configuration assistant.

Your task: Given emission category dimension values for a GHG reporting category, suggest meaningful column names for a data entry form.

Context:
- The GHG category is: "{body.category_name}"
- The emission factor unit is: "{body.denominator_unit}"
- Emission category names were split by " - " into dimensions.
- This category has MULTIPLE dimension patterns: {dim_counts} dimensions.
- Each dimension group needs its own set of column names.
- There are already existing columns in the system: {json.dumps(body.existing_columns)}

Rules:
1. For each dimension in each group, suggest a clear, concise column name (2-4 words max).
2. If an existing column name matches well, set "reuse_existing" to that exact name.
3. Column names should be CONSISTENT across groups where the same concept appears (e.g., if both 2-dim and 3-dim groups have a "disposal method" dimension, use the same name).
4. For the activity/numeric column, suggest a name based on the unit (e.g., "Weight" for tonnes/kg, "Volume" for litres, "Distance" for km).
5. If the unit suggests the data can also be entered in related units, list them in "suggested_units".

Return a JSON object with this exact structure:
{{
  "groups": [
    {{
      "dim_count": 2,
      "columns": [
        {{ "position": 0, "suggested_name": "Waste Type", "reuse_existing": "Waste Type" }},
        {{ "position": 1, "suggested_name": "Disposal Method", "reuse_existing": null }}
      ],
      "activity_column_name": "Weight"
    }},
    {{
      "dim_count": 3,
      "columns": [
        {{ "position": 0, "suggested_name": "Material Category", "reuse_existing": null }},
        {{ "position": 1, "suggested_name": "Material Type", "reuse_existing": null }},
        {{ "position": 2, "suggested_name": "Disposal Method", "reuse_existing": null }}
      ],
      "activity_column_name": "Weight"
    }}
  ],
  "suggested_units": ["ton", "kg", "tonnes"]
}}

Return ONLY the JSON object, no other text."""
