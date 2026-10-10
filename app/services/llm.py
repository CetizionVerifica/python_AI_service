
import json
import logging
import threading
import time
from typing import Any, Dict
from openai import OpenAI
from app.core.config import settings
from app.schemas.invoice import InvoiceData
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


# Configure OpenRouter client via OpenAI SDK. Every call is bounded by
# LLM_TIMEOUT_S so a hung request can't hold a worker indefinitely; retries
# are done once, by the tenacity decorator on _call_openrouter (MAX_RETRIES),
# not again inside the SDK.
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.OPENROUTER_API_KEY,
    timeout=settings.LLM_TIMEOUT_S,
    max_retries=0,
    default_headers={
        "HTTP-Referer": settings.OPENROUTER_REFERER,
        "X-Title": settings.OPENROUTER_TITLE,
    },
)

# Model to use — pick a fast, capable model for structured extraction
MODEL_NAME = settings.OPENROUTER_MODEL


class LLMError(RuntimeError):
    pass


# At most MAX_CONCURRENT_LLM calls in flight per process; a caller waits up
# to LLM_TIMEOUT_S for a slot, then gets an LLMError instead of queueing forever.
_llm_slots = threading.BoundedSemaphore(max(1, settings.MAX_CONCURRENT_LLM))


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


def _build_column_config_prompt(column_config: dict) -> str:
    """
    Builds a prompt section describing the dropdown fields that the LLM should
    extract values for, based on the column_config's select-type columns.
    """
    columns = column_config.get("columns", [])
    column_options = column_config.get("column_options", {})
    column_dependencies = column_config.get("column_dependencies", {})
    dependent_options = column_config.get("dependent_options", {})

    # Find select-type columns that have dropdown options
    select_columns = []
    col_id_to_name = {}
    for col in columns:
        col_id_to_name[str(col["pk_id"])] = col["column_name"]
        if col["column_type"] == "select":
            select_columns.append(col)

    if not select_columns:
        return ""

    # Identify parent and child columns
    child_cols = set(column_dependencies.keys())
    parent_cols = set(column_dependencies.values())

    lines = [
        "",
        "IMPORTANT — This category has configurable dropdown fields. "
        "For each activity, you MUST also extract values for these fields and return them "
        "in a `column_values` object inside each activity.",
        "",
    ]

    # Describe parent/independent columns first
    for col in select_columns:
        col_name = col["column_name"]
        col_id = str(col["pk_id"])
        options = column_options.get(col_id, [])

        if col_name in child_cols and not options:
            # This is a pure dependent column — described below with its parent
            continue

        if options:
            option_labels = [opt["label"] for opt in options]
            lines.append(f'Field "{col_name}" — select EXACTLY one of:')
            for label in option_labels:
                lines.append(f"  - {label}")
            lines.append("")

    # Describe dependent columns with their parent relationships
    for child_name, parent_name in column_dependencies.items():
        dep_opts = dependent_options.get(child_name, {})
        if dep_opts:
            lines.append(
                f'Field "{child_name}" — depends on "{parent_name}". '
                f"Select one based on the chosen {parent_name}:"
            )
            for parent_val, child_options in dep_opts.items():
                child_labels = [opt["label"] for opt in child_options]
                lines.append(f'  If {parent_name} = "{parent_val}": {", ".join(child_labels)}')
            lines.append("")

    lines.append(
        'Return these values in each activity\'s "column_values" field as '
        '{"Field Name": "Option Label"}. Use the EXACT labels listed above. '
        "If you cannot determine a field's value from the document, omit it."
    )

    return "\n".join(lines)


def build_extraction_prompt(
    known_categories: list[str] | None = None,
    available_units: list[str] | None = None,
    column_config: dict | None = None,
) -> str:
    """
    Builds the extraction system prompt.
    When known_categories is provided, the LLM is instructed to identify which
    emission category name from that list best fits each invoice.
    When available_units is provided, the LLM maps the raw document unit to the
    closest configured unit name for accurate downstream calculation.
    When column_config is provided, the LLM also extracts dropdown field values.
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
        "- For `unit_of_measurement`: extract the physical unit EXACTLY as it appears in the document (e.g., \"litre\", \"kg\", \"kWh\", \"m³\", \"gallon\", \"tonne\"). NOT currency.\n"
        "  CRITICAL: kWh (kilowatt-hours, real energy) and kVAh (kilovolt-ampere-hours, apparent energy) are DIFFERENT units. Do NOT treat them as equivalent. Extract exactly what the document shows."
        if not available_units
        else (
            "- For `unit_of_measurement`: identify the physical quantity unit in the document, "
            "then return the EXACT name from the configured units list below that best matches it. "
            "For example if the document says \"kgs\" and the list contains \"kg\", return \"kg\". "
            "Only use the raw document value if nothing in the list matches. NOT currency.\n"
            "  CRITICAL: kWh and kVAh are DIFFERENT units — do NOT convert or substitute one for the other. "
            "If the invoice shows kVAh but the configured list only has kWh, return the raw value \"kVAh\" from the document.\n"
            "  Configured activity units:\n"
            + "\n".join(f"    - {u}" for u in available_units)
        )
    )

    # Build column_config dropdown section
    column_config_section = ""
    column_values_schema = ""
    if column_config:
        column_config_section = _build_column_config_prompt(column_config)
        if column_config_section:
            column_values_schema = ',\n                "column_values": {{"Field Name": "Option Label or null"}}'

    return f"""You are an expert data extraction assistant.
Your task is to extract structured invoice data from the provided text.
The document may contain ONE or MULTIPLE invoices/bills. Extract each one separately.

Rules:
- Do not guess. If a field is not found, return null.
- Extract `subtotal`, `tax_amount`, and `total_amount` carefully.
- Format dates as YYYY-MM-DD.
- For `billing_month_end`: find the billing period or billing month (e.g., "Bill for the Month of April 2025", "Billing Period: March 2025").
  Extract the LAST DAY of that billing month in YYYY-MM-DD format (e.g., "April 2025" → "2025-04-30", "February 2024" → "2024-02-29").
  This is NOT the invoice issue date — it is the end of the consumption/service period.
  If no billing month/period is mentioned, return null.
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
  CRITICAL for refrigerant gases: Extract the EXACT gas designation as printed on the invoice (R-22, R-32, R-134a, R-410a, HFC-32, etc.). Do NOT confuse similar designations — R-22 and R-32 are completely different chemicals. Read the number carefully from the document text.
- For each activity's `total_quantity`: extract the total physical quantity (not monetary). Sum line item quantities if needed.
  For electricity bills: prefer kWh (real energy consumed) over kVAh (apparent energy). Look for fields labeled "Units Consumed", "Energy Consumed", "kWh", or "Total Units" rather than "kVAh" or "Demand" values. Only use kVAh if kWh is not present.
{unit_rule}
{emission_category_rule}
{column_config_section}

Return a JSON object with key "invoices" containing an array. Each element is one invoice:
{{"invoices": [
    {{
        "invoice_number": "string or null",
        "invoice_date": "YYYY-MM-DD or null",
        "billing_month_end": "YYYY-MM-DD or null (last day of the billing month/period)",
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
                "emission_category": "string or null"{column_values_schema}
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


def _attempt(messages: list[dict], slot_wait_s: float, deadline: float | None = None) -> str:
    """One OpenRouter call. With a deadline (time.monotonic()), the request gets
    whatever time is left after waiting for a slot; otherwise LLM_TIMEOUT_S."""
    if not _llm_slots.acquire(timeout=slot_wait_s):
        logger.warning("OpenRouter call skipped: all LLM slots busy")
        raise LLMError("The AI service is busy. Please try again shortly.")
    try:
        timeout_s = settings.LLM_TIMEOUT_S if deadline is None else deadline - time.monotonic()
        if timeout_s <= 0.1:
            raise LLMError("The AI service is busy. Please try again shortly.")
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=13333,
            timeout=timeout_s,
        )
        return response.choices[0].message.content
    except LLMError:
        raise
    except Exception as e:
        logger.warning(f"OpenRouter call failed: {e}")
        raise e
    finally:
        _llm_slots.release()


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(max(0, settings.MAX_RETRIES) + 1),
)
def _call_openrouter(messages: list[dict]) -> str:
    """
    Calls OpenRouter via the OpenAI SDK with retry logic. Each attempt times
    out after LLM_TIMEOUT_S.
    """
    return _attempt(messages, settings.LLM_TIMEOUT_S)


def call_openrouter_within(messages: list[dict], budget_s: float) -> str:
    """
    One OpenRouter call that finishes, slot wait included, within budget_s,
    for callers that give up after a fixed time. No retries: the caller's own
    retry (a new request) gets a fresh budget.
    """
    return _attempt(messages, budget_s, time.monotonic() + budget_s)


def extract_structured_data(
    ocr_text: str,
    known_categories: list[str] | None = None,
    available_units: list[str] | None = None,
    column_config: dict | None = None,
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

    column_config: optional dict with column details, dropdown options, and
    dependencies. When supplied, the LLM also extracts dropdown field values.
    """
    prompt = build_extraction_prompt(known_categories, available_units, column_config)
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
