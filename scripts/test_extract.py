#!/usr/bin/env python3
"""
Test script for the OCR Invoice API.

Usage:
    # Test a single file against the legacy extract endpoint (no DB/Cloudinary):
    python scripts/test_extract.py tests/image.png

    # Test a single file against the full upload flow (DB + Cloudinary + OCR):
    python scripts/test_extract.py "sample-bills/DIESEL bills.pdf" --upload

    # Test ALL sample bills (extract only):
    python scripts/test_extract.py --all

    # Test ALL sample bills (full upload flow):
    python scripts/test_extract.py --all --upload

    # Custom server URL:
    python scripts/test_extract.py --all --base-url http://localhost:9000
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("httpx is required. Install it: pip install httpx")
    sys.exit(1)


DEFAULT_BASE_URL = "http://localhost:8000"
SAMPLE_BILLS_DIR = Path(__file__).resolve().parent.parent / "sample-bills"

# Colors for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_header(text: str):
    print(f"\n{BOLD}{CYAN}{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}{RESET}\n")


def print_result(label: str, value, color: str = ""):
    if value is not None:
        print(f"  {color}{label}: {RESET}{value}")


def extract_invoice(base_url: str, file_path: Path, category: str = "general") -> dict:
    """Call the legacy POST /v1/extract endpoint."""
    url = f"{base_url}/v1/extract"
    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f)}
        data = {"category": category}
        response = httpx.post(url, files=files, data=data, timeout=120.0)
    response.raise_for_status()
    return response.json()


def upload_invoice(
    base_url: str,
    file_path: Path,
    category: str = "general",
    site_id: int | None = None,
    category_id: int | None = None,
    uploaded_by: int | None = None,
) -> dict:
    """Call the full POST /v1/invoices/upload endpoint."""
    url = f"{base_url}/v1/invoices/upload"
    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f)}
        data = {"category": category}
        if site_id is not None:
            data["site_id"] = str(site_id)
        if category_id is not None:
            data["category_id"] = str(category_id)
        if uploaded_by is not None:
            data["uploaded_by"] = str(uploaded_by)
        response = httpx.post(url, files=files, data=data, timeout=120.0)
    response.raise_for_status()
    return response.json()


def display_result(result: dict, elapsed: float):
    """Pretty-print the extraction result."""
    data = result.get("data") or {}

    print_result("Filename", result.get("filename"), BOLD)
    print_result("Category", result.get("category"))
    print_result("Time", f"{elapsed:.1f}s")
    print()

    if result.get("error"):
        print_result("⚠ Error", result["error"], RED)
        print()

    # Invoice fields
    print_result("Invoice #", data.get("invoice_number"), GREEN)
    print_result("Date", data.get("invoice_date"), GREEN)
    print_result("Vendor", data.get("vendor_name"), GREEN)
    print_result("Subtotal", data.get("subtotal"))
    print_result("Tax", data.get("tax_amount"))
    print_result("Total", data.get("total_amount"), BOLD)
    print_result("Currency", data.get("currency"))

    # Emission-relevant fields
    print()
    print_result("🏭 Activity", data.get("activity_description"), YELLOW)
    print_result("📦 Quantity", data.get("total_quantity"), YELLOW)
    print_result("📐 Unit", data.get("unit_of_measurement"), YELLOW)

    # Line items
    line_items = data.get("line_items", [])
    if line_items:
        print(f"\n  {CYAN}Line Items ({len(line_items)}):{RESET}")
        for i, item in enumerate(line_items, 1):
            desc = item.get("description", "?")
            qty = item.get("quantity", "?")
            unit = item.get("unit", "")
            price = item.get("unit_price", "?")
            amt = item.get("amount", "?")
            unit_str = f" {unit}" if unit else ""
            print(f"    {i}. {desc} — {qty}{unit_str} × {price} = {amt}")

    # Validations
    validations = result.get("validations", [])
    if validations:
        print(f"\n  {CYAN}Validations:{RESET}")
        for v in validations:
            status = f"{GREEN}✓ PASS{RESET}" if v.get("ok") else f"{RED}✗ FAIL{RESET}"
            print(f"    {status} {v.get('check')}")
            if not v.get("ok"):
                print(f"      Expected: {v.get('expected')}, Got: {v.get('total')}, Delta: {v.get('delta')}")


def test_single(base_url: str, file_path: Path, use_upload: bool):
    """Test a single file."""
    print_header(f"Testing: {file_path.name}")

    if not file_path.exists():
        print(f"{RED}File not found: {file_path}{RESET}")
        return False

    start = time.time()
    try:
        if use_upload:
            print(f"  Mode: {YELLOW}Full Upload (Cloudinary + DB + OCR){RESET}")
            result = upload_invoice(base_url, file_path)
        else:
            print(f"  Mode: {YELLOW}Extract Only (no storage){RESET}")
            result = extract_invoice(base_url, file_path)

        elapsed = time.time() - start
        display_result(result, elapsed)
        print(f"\n  {GREEN}✓ SUCCESS{RESET}")
        return True

    except httpx.HTTPStatusError as e:
        elapsed = time.time() - start
        print(f"  {RED}✗ HTTP Error {e.response.status_code}: {e.response.text[:200]}{RESET}")
        print(f"  Time: {elapsed:.1f}s")
        return False
    except Exception as e:
        elapsed = time.time() - start
        print(f"  {RED}✗ Error: {e}{RESET}")
        print(f"  Time: {elapsed:.1f}s")
        return False


def test_all(base_url: str, use_upload: bool):
    """Test all PDFs in sample-bills/."""
    if not SAMPLE_BILLS_DIR.exists():
        print(f"{RED}sample-bills/ directory not found{RESET}")
        sys.exit(1)

    files = sorted(SAMPLE_BILLS_DIR.glob("*.pdf"))
    if not files:
        print(f"{RED}No PDF files found in sample-bills/{RESET}")
        sys.exit(1)

    print_header(f"Testing {len(files)} sample bills")
    print(f"  Mode: {'Full Upload' if use_upload else 'Extract Only'}")
    print(f"  Server: {base_url}\n")

    results = []
    total_start = time.time()

    for file_path in files:
        success = test_single(base_url, file_path, use_upload)
        results.append((file_path.name, success))

    total_elapsed = time.time() - total_start

    # Summary
    passed = sum(1 for _, s in results if s)
    failed = sum(1 for _, s in results if not s)

    print_header("SUMMARY")
    for name, success in results:
        status = f"{GREEN}✓ PASS{RESET}" if success else f"{RED}✗ FAIL{RESET}"
        print(f"  {status}  {name}")

    print(f"\n  Total: {len(results)} | {GREEN}Passed: {passed}{RESET} | {RED}Failed: {failed}{RESET}")
    print(f"  Total time: {total_elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(description="Test the OCR Invoice API")
    parser.add_argument("file", nargs="?", help="Path to invoice file to test")
    parser.add_argument("--all", action="store_true", help="Test all PDFs in sample-bills/")
    parser.add_argument("--upload", action="store_true", help="Use full upload flow (DB + Cloudinary) instead of extract-only")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Server URL (default: {DEFAULT_BASE_URL})")

    args = parser.parse_args()

    if not args.file and not args.all:
        parser.print_help()
        print(f"\n{YELLOW}Example:{RESET}")
        print(f'  python scripts/test_extract.py "sample-bills/DIESEL bills.pdf"')
        print(f"  python scripts/test_extract.py --all")
        sys.exit(1)

    # Health check
    try:
        r = httpx.get(f"{args.base_url}/health", timeout=5.0)
        r.raise_for_status()
        print(f"{GREEN}✓ Server is running at {args.base_url}{RESET}")
    except Exception:
        print(f"{RED}✗ Cannot reach server at {args.base_url}{RESET}")
        print(f"  Make sure the server is running: python -m app.main")
        sys.exit(1)

    if args.all:
        test_all(args.base_url, args.upload)
    else:
        file_path = Path(args.file)
        if not file_path.is_absolute():
            # Resolve relative to project root
            file_path = Path(__file__).resolve().parent.parent / file_path
        test_single(args.base_url, file_path, args.upload)


if __name__ == "__main__":
    main()
