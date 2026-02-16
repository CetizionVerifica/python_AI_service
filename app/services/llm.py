
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


EXTRACTION_PROMPT = """You are an expert data extraction assistant.
Your task is to extract structured invoice data from the provided text.
The document may contain ONE or MULTIPLE invoices/bills. Extract each one separately.

Rules:
- Do not guess. If a field is not found, return null.
- Extract `subtotal`, `tax_amount`, and `total_amount` carefully.
- Format dates as YYYY-MM-DD.
- For `activity_description`: identify what resource/commodity was consumed (e.g., "Diesel", "Electricity", "LPG", "Coal", "R-22", "Water", "Waste").
- For `total_quantity`: extract the total physical quantity consumed (not monetary). Sum line item quantities if needed.
- For `unit_of_measurement`: extract the physical unit (e.g., "litre", "kg", "kWh", "m³", "gallon", "tonne"). NOT currency.

Return a JSON object with key "invoices" containing an array. Each element is one invoice:
{"invoices": [
    {
        "invoice_number": "string or null",
        "invoice_date": "YYYY-MM-DD or null",
        "vendor_name": "string or null",
        "vendor_address": "string or null",
        "subtotal": "float or null",
        "tax_amount": "float or null",
        "total_amount": "float or null",
        "currency": "INR",
        "activity_description": "string or null",
        "total_quantity": "float or null",
        "unit_of_measurement": "string or null",
        "line_items": [
            {
                "description": "string",
                "quantity": "float",
                "unit": "string or null",
                "unit_price": "float",
                "amount": "float"
            }
        ]
    }
]}

If there is only one invoice, still return it inside the array."""


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
            max_tokens=4096,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.warning(f"OpenRouter call failed, retrying... Error: {e}")
        raise e


def extract_structured_data(ocr_text: str) -> list[InvoiceData]:
    """
    Sends the OCR text to OpenRouter to extract structured Invoice Data.
    Returns a list of invoices (1 or more per document).
    """
    messages = [
        {"role": "system", "content": EXTRACTION_PROMPT},
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

        # Validate each with Pydantic
        return [InvoiceData(**inv) for inv in invoices_raw]

    except (LLMError, ValueError, ValidationError) as e:
        logger.error(f"Extraction Error: {e}")
        raise e
    except Exception as e:
        logger.exception("Unexpected LLM Failure")
        raise e
