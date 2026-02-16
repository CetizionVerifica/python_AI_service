
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List
from app.schemas.invoice import InvoiceData

_MONEY_RE = re.compile(r"-?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        match = _MONEY_RE.search(value.replace("\u00a0", " "))
        if not match:
            return None
        cleaned = match.group(0).replace(",", "").replace(" ", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def validate_totals(invoice: InvoiceData) -> List[Dict[str, Any]]:
    # Access fields from Pydantic model
    subtotal = _to_decimal(invoice.subtotal)
    tax = _to_decimal(invoice.tax_amount)
    total = _to_decimal(invoice.total_amount)
    
    if subtotal is None or tax is None or total is None:
        return []

    # Check: Subtotal + Tax == Total
    expected = subtotal + tax
    delta = abs(total - expected)
    
    # Allow 0.02 currency unit tolerance
    ok = delta <= Decimal("0.02")
    
    return [
        {
            "check": "subtotal_plus_tax_equals_total",
            "ok": ok,
            "subtotal": float(subtotal),
            "tax": float(tax),
            "total": float(total),
            "expected": float(expected),
            "delta": float(delta),
        }
    ]
