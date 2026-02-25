
import json
import logging
from typing import Any, Dict
from openai import OpenAI
from app.core.config import settings
from app.schemas.invoice import InvoiceData
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


# Configure OpenRouter client via OpenAI SDK
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": settings.OPENROUTER_REFERER,
        "X-Title": settings.OPENROUTER_TITLE,
    },
)

# Model to use — pick a fast, capable model for structured extraction
MODEL_NAME = settings.OPENROUTER_MODEL


class LLMError(RuntimeError):
    pass


def _clean_json_text(text: str) -> str:
    """
    Cleans MarkDown code fences from LLM response.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return cleaned


def _parse_json_object(text: str) -> dict | list:
    cleaned = _clean_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: try to find a JSON object { ... }
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        # Fallback: try to find a JSON array [ ... ]
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise LLMError("LLM returned invalid JSON.")


_ACTIVITY_FIELDS = ("activity_description", "total_quantity", "unit_of_measurement", "emission_category")


def _normalize_invoice_activities(inv_dict: dict) -> dict:
    """
    Ensure the activities array exists. If the LLM returns old-format flat
    activity fields instead, migrate them into a single-element activities list.
    """
    if "activities" not in inv_dict or not inv_dict["activities"]:
        activity = {}
        for field in _ACTIVITY_FIELDS:
            if field in inv_dict and inv_dict[field] is not None:
                activity[field] = inv_dict[field]
        inv_dict["activities"] = [activity] if activity else []
    # Remove flat fields so they don't conflict with computed_field on InvoiceData
    for field in _ACTIVITY_FIELDS:
        inv_dict.pop(field, None)
    return inv_dict


def build_extraction_prompt(
    known_categories: list[str] | None = None,
    available_units: list[str] | None = None,
) -> str:
    """
    Builds the extraction system prompt.
    When known_categories is provided, the LLM is instructed to identify which
    emission category name from that list best fits each invoice.
    When available_units is provided, the LLM maps the raw document unit to the
    closest configured unit name for accurate downstream calculation.
    """
    emission_category_rule = (
        "- For `emission_category`: return null — it will be determined automatically."
        if not known_categories
        else (
            "- For `emission_category`: scan the document for text that matches or closely "
            "describes one of the valid emission category names listed below. "
            "Return the EXACT name from the list that best fits this invoice. "
            "If nothing matches, return null.\n"
            "  Valid emission categories:\n"
            + "\n".join(f"    - {c}" for c in known_categories)
        )
    )

    unit_rule = (
        "- For `unit_of_measurement`: extract the physical unit EXACTLY as it appears in the document (e.g., \"litre\", \"kg\", \"kWh\", \"m³\", \"gallon\", \"tonne\"). NOT currency."
        if not available_units
        else (
            "- For `unit_of_measurement`: identify the physical quantity unit in the document, "
            "then return the EXACT name from the configured units list below that best matches it. "
            "For example if the document says \"kgs\" and the list contains \"kg\", return \"kg\". "
            "Only use the raw document value if nothing in the list matches. NOT currency.\n"
            "  Configured activity units:\n"
            + "\n".join(f"    - {u}" for u in available_units)
        )
    )

    return f"""You are an expert data extraction assistant.
Your task is to extract structured invoice data from the provided text.
The document may contain ONE or MULTIPLE invoices/bills. Extract each one separately.

Rules:
- Do not guess. If a field is not found, return null.
- Extract `subtotal`, `tax_amount`, and `total_amount` carefully.
- Format dates as YYYY-MM-DD.
- For `activities`: identify ALL distinct purchased items or resources in each invoice.
  Each line item that has its own quantity and unit should be a separate activity entry.
  Include consumables (fuel, gas, chemicals), equipment (cylinders, containers), materials,
  and any other billable item with a measurable quantity — exclude only pure service charges
  like shipping, carriage, or taxes that have no physical unit.
  For example, if an invoice lists "Refrigerant Gas HFC-32 10 KG", "R-410a Gas 45 KG",
  and "Empty Cylinders 1 No", return three activity entries.
  If only one item was purchased, still return it inside the activities array.
  If no activity can be identified, return an empty activities array.
- For each activity's `activity_description`: identify what was purchased (e.g., "Diesel", "Electricity", "LPG", "Coal", "R-22", "Water", "Waste", "Empty Cylinders", "R-410a Gas").
- For each activity's `total_quantity`: extract the total physical quantity (not monetary). Sum line item quantities if needed.
{unit_rule}
{emission_category_rule}

Return a JSON object with key "invoices" containing an array. Each element is one invoice:
{{"invoices": [
    {{
        "invoice_number": "string or null",
        "invoice_date": "YYYY-MM-DD or null",
        "vendor_name": "string or null",
        "vendor_address": "string or null",
        "subtotal": "float or null",
        "tax_amount": "float or null",
        "total_amount": "float or null",
        "currency": "string",
        "activities": [
            {{
                "activity_description": "string or null",
                "total_quantity": "float or null",
                "unit_of_measurement": "string or null",
                "emission_category": "string or null"
            }}
        ],
        "line_items": [
            {{
                "description": "string",
                "quantity": "float",
                "unit": "string or null",
                "unit_price": "float",
                "amount": "float"
            }}
        ]
    }}
]}}

If there is only one invoice, still return it inside the array.
If there is only one activity per invoice, still return it inside the activities array."""


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
)
def _call_openrouter(messages: list[dict]) -> str:
    """
    Calls OpenRouter via the OpenAI SDK with retry logic.
    """
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=13333,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.warning(f"OpenRouter call failed, retrying... Error: {e}")
        raise e


def extract_structured_data(
    ocr_text: str,
    known_categories: list[str] | None = None,
    available_units: list[str] | None = None,
) -> list[InvoiceData]:
    """
    Sends the OCR text to OpenRouter to extract structured Invoice Data.
    Returns a list of invoices (1 or more per document).

    known_categories: optional list of emission_category_name values scoped to
    the upload's site+category combination. When supplied, the LLM is guided to
    identify which category best fits the invoice content.

    available_units: optional list of configured activity unit names (e.g. "kg",
    "litres"). When supplied, the LLM maps the raw document unit to the closest
    match so the extracted value aligns with the configured units for calculation.
    """
    prompt = build_extraction_prompt(known_categories, available_units)
    if known_categories:
        logger.info(
            f"LLM prompt includes {len(known_categories)} known emission categories: "
            f"{known_categories}"
        )
    if available_units:
        logger.info(
            f"LLM prompt includes {len(available_units)} configured activity units: {available_units}"
        )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"DOCUMENT TEXT:\n{ocr_text}"},
    ]

    try:
        logger.info(f"Sending request to OpenRouter ({MODEL_NAME})...")
        raw_response = _call_openrouter(messages)
        logger.info("Received response from OpenRouter.")

        json_data = _parse_json_object(raw_response)

        # Normalize to list of invoice dicts
        if isinstance(json_data, dict):
            invoices_raw = json_data.get("invoices", [json_data])
        elif isinstance(json_data, list):
            invoices_raw = json_data
        else:
            raise LLMError("Unexpected LLM response format.")

        if not invoices_raw:
            raise LLMError("LLM returned no invoices.")

        # Normalize activities and validate each with Pydantic
        return [InvoiceData(**_normalize_invoice_activities(inv)) for inv in invoices_raw]

    except (LLMError, ValueError, ValidationError) as e:
        logger.error(f"Extraction Error: {e}")
        raise e
    except Exception as e:
        logger.exception("Unexpected LLM Failure")
        raise e
