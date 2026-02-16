# OCR Invoice — Context Log

## 2026-02-15: Server & Script Verification

### Findings

1. **Server runs on port 8000** — `.env` has `PORT=8080` but `main.py` reads `os.getenv("PORT", "8000")` and the `.env` is NOT loaded by `main.py` (only by `app/core/config.py` via pydantic-settings). The PORT env var is not part of the `Settings` class, so it's never injected.

2. **Health endpoint OK** — `GET /health` returns `{"status":"ok"}`.

3. **Test script works** — `scripts/test_extract.py` successfully tests the extract endpoint. Supports `--all`, `--upload`, and `--base-url` flags.

4. **LLM bug on multi-invoice PDFs** — When the LLM returns a JSON **list** (e.g., `[{...}]`) instead of a single object, `InvoiceData(**json_data)` fails with `argument after ** must be a mapping, not list`. The fallback regex extraction catches it and returns partial data.

5. **Successful extraction** — `Electricity Bill APRIL 2025.pdf` extracted fully in 6.3s: 8 line items, emission fields (Electricity, 378360 kVAh). Validation caught a subtotal+tax mismatch (expected 3355614.6, got 3020695.0).

6. **DB empty** — `GET /v1/invoices` returns `[]` (no records stored since extract-only mode was used).

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/v1/extract` | Legacy extract-only (no DB/Cloudinary) |
| POST | `/v1/invoices/upload` | Full upload flow (Cloudinary + DB + OCR) |
| GET | `/v1/invoices` | List invoices (optional `site_id`, `category_id` filters) |
| GET | `/v1/invoices/{id}` | Get single invoice |
| DELETE | `/v1/invoices/{id}` | Delete invoice |
| DELETE | `/v1/invoices/bulk` | Bulk delete invoices |

---

### Fix: LLM list-response handling (`app/services/llm.py`)

- `_parse_json_object()` now handles both `dict` and `list` JSON responses (fallback bracket search for `[...]` added).
- `extract_structured_data()` unwraps the first element if the LLM returns a list instead of a single object.
- Server needs restart to pick up changes (running without `--reload`).

### Full Test Run: All 13 Sample Bills — 13/13 PASS (83.7s)

| # | File | Time | Activity | Quantity | Validation |
|---|------|------|----------|----------|------------|
| 1 | COAL April bills.pdf | 9.1s | Coal | 25.81 MT | ✗ (Δ0.46) |
| 2 | COAL December bills.pdf | 9.8s | Coal | 20.0 MT | ✓ |
| 3 | COAL July bills.pdf | 8.1s | Coal | 26.3 MT | ✓ |
| 4 | DIESEL bills.pdf | 6.2s | Diesel | 1600.0 L | ✓ |
| 5 | Electricity Bill APRIL 2025.pdf | 6.4s | Electricity | 378360 kVAh | ✗ (Δ357621) |
| 6 | Electricity Bill Dec2025.pdf | 4.6s | Electricity | 377603 kWh | ✗ (Δ1999.87) |
| 7 | Electricity Bill JULY 2025.pdf | 5.1s | Electricity | 486340 kVAh | ✗ (Δ246720) |
| 8 | R-22 bills.pdf | 6.2s | Natural Gas | 100.0 kg | ✓ |
| 9 | R-32 bills.pdf | 6.9s | Refrigerant Gas | 10.0 kg | ✓ |
| 10 | WATER Bill April.pdf | 3.9s | Water | 2456 KL | ✓ |
| 11 | Waste disposal bills.pdf | 9.0s | Spent Carbon | 3.16 tonne | ✓ |
| 12 | Water Bill December.pdf | 4.3s | Water | 2563 KL | ✓ |
| 13 | Water Bill July.pdf | 4.2s | Water | 2714 KL | ✓ |

### Fix: 402 Credit Exhaustion (`app/services/llm.py`)

- Added `max_tokens=4096` to the OpenRouter API call. Previously defaulted to 65,535 (model max), making each request ~16x more expensive than needed for structured JSON output.

### Full Test Run #2 (post max_tokens fix): 13/13 OK, 0 Fallbacks (112.3s)

- All 13 bills extracted via LLM successfully (no fallback needed).
- Results saved as individual JSON files in `test-results/` plus `_summary.json`.
- Created `scripts/test_all_json.py` for JSON output testing.

---

## 2026-02-16: Emission Data Mapping

### Changes
- `EmissionReady` schema: added `site_id`, `total_emission` (default 0), `unit` (default "kg CO2e")
- `pipeline.py`: accepts `site_id`/`category_id`, maps invoice fields → emission structure
- `invoices.py`: passes `site_id`/`category_id` from upload request to pipeline
- Fixed crash bug: `result.data.model_dump()` → `[inv.model_dump() for inv in result.data]` (data is now a list)

### Emission Field Mapping
| Emission Key | Source |
|---|---|
| `site_id` | Request form data |
| `category_id` | Request form data (fallback: fuzzy match suggestion) |
| `activity_data["Activity Data"]` | `invoice.total_quantity` |
| `activity_data["emission_category"]` | Fuzzy match or `invoice.activity_description` |
| `activity_data_unit` | `invoice.unit_of_measurement` (or suggestion `denominator_unit`) |
| `date_of_reporting` | `invoice.invoice_date` |
| `total_emission` | 0.0 (to be calculated) |
| `unit` | "kg CO2e" |

### Test Run: 13/13 OK (102.9s)
Multi-invoice extraction now working — e.g., DIESEL PDF → 3 separate emission entries.
