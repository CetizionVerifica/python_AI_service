
import re
import logging
from typing import Optional
from app.schemas.invoice import InvoiceData

logger = logging.getLogger(__name__)

# Basic Regex Patterns
# Adjust these based on common invoice formats
DATE_PATTERN = re.compile(r"(?:Invoice|Order|Due)\s*Date[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE)
TOTAL_PATTERN = re.compile(r"(?:Total|Amount|Payable|Due).*?([£$€]?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.IGNORECASE)
INVOICE_NO_PATTERN = re.compile(r"(?:Invoice|Order)\s*(?:#|No\.?|Number)[:\s]*([A-Z0-9\-]+)", re.IGNORECASE)
VENDOR_PATTERN = re.compile(r"^([A-Z0-9\s&\.,]+)(?:\r?\n)", re.MULTILINE) # Rough guess: First line? Often inaccurate but better than nothing for some layouts.

def extract_fallback_data(text: str) -> InvoiceData:
    """
    Attempts to extract basic fields using Regex when LLM fails.
    """
    logger.info("Attempting regex fallback extraction...")
    
    data = InvoiceData()
    
    # 1. Total Amount
    # Find all matches, pick the largest one that looks like a total? Or the last one? 
    # Usually "Total" appears at the bottom.
    total_matches = TOTAL_PATTERN.findall(text)
    if total_matches:
        # Clean currency symbols
        try:
            # Get last match (often the final total)
            val_str = total_matches[-1].replace('£', '').replace('$', '').replace('€', '').replace(',', '').strip()
            data.total_amount = float(val_str)
        except ValueError:
            pass

    # 2. Date
    date_match = DATE_PATTERN.search(text)
    if date_match:
        data.invoice_date = date_match.group(1)

    # 3. Invoice Number
    inv_match = INVOICE_NO_PATTERN.search(text)
    if inv_match:
        data.invoice_number = inv_match.group(1)
        
    # 4. Currency (Guess based on symbol)
    if "£" in text:
        data.currency = "GBP"
    elif "€" in text:
        data.currency = "EUR"
    elif "$" in text:
        data.currency = "USD"
        
    return data
