"""The regex fallback returns bill dates as YYYY-MM-DD or not at all (F-18)."""
import pytest


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Invoice Date: 03/04/2025\\nTotal 100.00", "2025-04-03"),
        ("Invoice Date: 3-4-25\\nTotal 100.00", "2025-04-03"),
        ("Invoice Date: 15 March 2025\\nTotal 100.00", "2025-03-15"),
        ("Invoice Date: 31/02/2025\\nTotal 100.00", None),
        ("Due Date: 99/99/9999", None),
        ("no date here", None),
    ],
)
def test_fallback_date_is_iso_or_none(text, expected):
    from app.services.fallback import extract_fallback_data

    assert extract_fallback_data(text.replace("\\n", "\n")).invoice_date == expected
