#!/usr/bin/env python3
"""
Run all sample bills through the extract endpoint and save each response as JSON
in test-results/.

Usage:
    python scripts/test_all_json.py [--base-url http://localhost:8000]
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
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_BILLS_DIR = PROJECT_ROOT / "sample-bills"
OUTPUT_DIR = PROJECT_ROOT / "test-results"


def main():
    parser = argparse.ArgumentParser(description="Test all sample bills and save JSON results")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Server URL (default: {DEFAULT_BASE_URL})")
    args = parser.parse_args()

    # Health check
    try:
        r = httpx.get(f"{args.base_url}/health", timeout=5.0)
        r.raise_for_status()
        print(f"✓ Server is running at {args.base_url}")
    except Exception:
        print(f"✗ Cannot reach server at {args.base_url}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)

    files = sorted(SAMPLE_BILLS_DIR.glob("*.pdf"))
    if not files:
        print("No PDF files found in sample-bills/")
        sys.exit(1)

    print(f"\nTesting {len(files)} sample bills → saving to test-results/\n")

    summary = []

    for file_path in files:
        name = file_path.stem  # filename without extension
        safe_name = name.replace(" ", "_").replace("-", "_").lower()
        out_file = OUTPUT_DIR / f"{safe_name}.json"

        print(f"  ⏳ {file_path.name} ...", end=" ", flush=True)
        start = time.time()

        try:
            with open(file_path, "rb") as f:
                response = httpx.post(
                    f"{args.base_url}/v1/extract",
                    files={"file": (file_path.name, f)},
                    data={"category": "general"},
                    timeout=120.0,
                )
            response.raise_for_status()
            result = response.json()
            elapsed = time.time() - start

            # Save JSON
            with open(out_file, "w") as out:
                json.dump(result, out, indent=2, ensure_ascii=False)

            has_error = bool(result.get("error"))
            status = "⚠️  FALLBACK" if has_error else "✓ OK"
            print(f"{status} ({elapsed:.1f}s) → {out_file.name}")

            summary.append({
                "file": file_path.name,
                "status": "fallback" if has_error else "ok",
                "time_s": round(elapsed, 1),
                "error": result.get("error"),
                "output": out_file.name,
            })

        except Exception as e:
            elapsed = time.time() - start
            print(f"✗ ERROR ({elapsed:.1f}s): {e}")
            summary.append({
                "file": file_path.name,
                "status": "error",
                "time_s": round(elapsed, 1),
                "error": str(e),
            })

    # Save summary
    summary_file = OUTPUT_DIR / "_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    # Print summary
    ok = sum(1 for s in summary if s["status"] == "ok")
    fallback = sum(1 for s in summary if s["status"] == "fallback")
    errors = sum(1 for s in summary if s["status"] == "error")
    total_time = sum(s["time_s"] for s in summary)

    print(f"\n{'='*50}")
    print(f"  ✓ OK: {ok}  |  ⚠️ Fallback: {fallback}  |  ✗ Error: {errors}")
    print(f"  Total time: {total_time:.1f}s")
    print(f"  Results saved to: {OUTPUT_DIR}/")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
