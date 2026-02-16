#!/usr/bin/env python3
"""
Test script to verify emission data extraction.
"""

import sys
import argparse
from pathlib import Path

try:
    import httpx
except ImportError:
    print("httpx is required. Install it: pip install httpx")
    sys.exit(1)

DEFAULT_BASE_URL = "http://localhost:8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_BILL = PROJECT_ROOT / "sample-bills" / "Electricity Bill APRIL 2025.pdf"

def main():
    parser = argparse.ArgumentParser(description="Test emission data extraction")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Server URL (default: {DEFAULT_BASE_URL})")
    args = parser.parse_args()

    if not SAMPLE_BILL.exists():
        print(f"Sample bill not found: {SAMPLE_BILL}")
        sys.exit(1)

    print(f"Testing emission extraction with {SAMPLE_BILL.name}...")
    print(f"Goal: Check if 'emission' field matches user requirements (site_id=16, category_id=6)\n")

    # We use the upload endpoint because that's what validates/stores, 
    # but strictly speaking the logic is in pipeline so even 'extract' should work if we updated it?
    # Actually 'extract' endpoint (legacy) does NOT accept site_id/category_id in the form (app/api/invoices.py:133).
    # So we MUST use /v1/invoices/upload to pass site_id/category_id.
    
    url = f"{args.base_url}/v1/invoices/upload"
    
    # We need to simulate the form data
    data = {
        "category": "general",
        "site_id": "16",
        "category_id": "6",
        "uploaded_by": "1"
    }
    
    try:
        with open(SAMPLE_BILL, "rb") as f:
            files = {"file": (SAMPLE_BILL.name, f)}
            response = httpx.post(url, data=data, files=files, timeout=60.0)
            
        if response.status_code != 200:
            print(f"Error: {response.status_code} - {response.text}")
            sys.exit(1)
            
        result = response.json()
        
        # Check emission field
        emissions = result.get("emission", [])
        if not emissions:
            print("❌ No 'emission' field found in response!")
            sys.exit(1)
            
        print("✅ Emission Data Received:")
        import json
        print(json.dumps(emissions, indent=2))
        
        # Verify specific fields
        first = emissions[0]
        if first.get("site_id") == 16 and first.get("category_id") == 6:
            print("\n✅ site_id and category_id match!")
        else:
            print(f"\n❌ ID mismatch: site_id={first.get('site_id')}, category_id={first.get('category_id')}")

        if "Activity Data" in first.get("activity_data", {}):
             print(f"✅ Activity Data extracted: {first['activity_data']['Activity Data']}")
        else:
             print("❌ Activity Data missing from activity_data dict")

    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
