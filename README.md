# OCR Invoice

FastAPI service that extracts structured data from bills/invoices (PDF or scanned images) using OCR + LLM, with emission-ready output for ESG reporting.

## Features

- **OCR** — Extracts text from PDFs (native + scanned via Tesseract)
- **LLM Extraction** — Sends OCR text to OpenRouter (Gemini) for structured data extraction
- **Multi-Invoice** — Handles documents with multiple invoices/bills
- **Emission Mapping** — Auto-maps extracted data to emission reporting format (`activity_data`, `date_of_reporting`, `activity_data_unit`)
- **Category Matching** — Fuzzy matches activity descriptions to emission categories (Coal, Diesel, Electricity, R-22, etc.)
- **Cloudinary + PostgreSQL** — Stores uploaded files and extraction results
- **Validation** — Cross-checks extracted totals against line items

## Setup

```bash
# Install dependencies
uv sync

# Copy and configure environment variables
cp .env.example .env

# Start server
uv run python main.py
```

Server runs on `http://localhost:8000` by default.

## Environment Variables

| Variable | Description |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USERNAME`, `DB_PASSWORD`, `DB_NAME` | PostgreSQL connection |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Cloudinary file storage |
| `OPENROUTER_API_KEY` | OpenRouter API key for LLM |
| `OPENROUTER_MODEL` | LLM model (default: `google/gemini-3-flash-preview`) |
| `AUTH_JWT_SECRET` | **Required.** Must equal ESG-lite's `JWT_SECRET`; verifies the sign-in token browsers send. The service does not start without it |
| `AI_SERVICE_KEY` | Shared key ESG-lite sends as `X-Service-Key` on its server-to-server calls. Empty disables those calls |

## Authentication

Every `/v1/*` route requires a caller; `/health` and `/` stay open.

- **Browser calls** send `Authorization: Bearer <ESG-lite token>` (the token ESG-lite returns at sign-in, HS256 with `JWT_SECRET`). The caller's role and sites are read from the shared ESG-lite tables (`"user"`, `user_sites`, `site`): Superadmin sees every site, an Admin every site of their company, a User or Manager their own site(s).
  - A `site_id` in the query, form or JSON body outside the caller's sites is refused with 403.
  - `GET /v1/invoices` returns only invoices at the caller's sites (invoices without a site: only to their uploader and Superadmin). Reading, serving, re-extracting or deleting another site's invoice answers 404.
  - `uploaded_by` / `user_id` sent by the client are ignored; the signed-in user is recorded.
  - The Excel import wizard (`/v1/excel/*`) only reads back documents the caller uploaded (Superadmin: any).
  - `/v1/emission-factors/*`, `/v1/category-mappings/*` and `/v1/emission-categories` are Superadmin only.
- **ESG-lite server-to-server calls** send `X-Service-Key: <AI_SERVICE_KEY>`. That key is accepted only on `/v1/column-config/*` and `/v1/sea-route`.

No token → 401; a valid caller without access → 403 (404 for a record outside their sites).

## API Endpoints

### Health Check

```
GET /health
```

### Extract Only (no storage)

```
POST /v1/extract
```

| Param | Type | Required | Description |
|---|---|---|---|
| `file` | File | ✅ | PDF or image file |

### Upload + Extract (full flow)

Uploads to Cloudinary, stores in DB, runs OCR → LLM → Validate → Emission Mapping.

```
POST /v1/invoices/upload
```

| Param | Type | Required | Description |
|---|---|---|---|
| `file` | File | ✅ | PDF or image file |
| `site_id` | int | No | Site ID for emission mapping |
| `category_id` | int | No | Emission category ID |
| `uploaded_by` | int | No | User ID |

### List Invoices

```
GET /v1/invoices?user_id=11&site_id=16
```

| Query Param | Type | Description |
|---|---|---|
| `user_id` | int | Filter by uploader |
| `site_id` | int | Filter by site |
| `category_id` | int | Filter by category |

### Get Invoice

```
GET /v1/invoices/{invoice_id}
```

### Delete Invoice

```
DELETE /v1/invoices/{invoice_id}
```

### Bulk Delete Invoices

```
DELETE /v1/invoices/bulk
```

```json
{ "ids": [1, 2, 3] }
```

## Response Structure

```json
{
  "filename": "DIESEL bills.pdf",
  "data": [
    {
      "invoice_number": "INV-001",
      "invoice_date": "2025-03-18",
      "vendor_name": "ABC Fuels",
      "total_amount": 120000.0,
      "activity_description": "Diesel",
      "total_quantity": 1600.0,
      "unit_of_measurement": "litre",
      "line_items": [...]
    }
  ],
  "emission": [
    {
      "site_id": 16,
      "category_id": 1,
      "activity_data": {
        "Activity Data": "1600.0",
        "emission_category": "Diesel"
      },
      "activity_data_unit": "litre",
      "date_of_reporting": "2025-03-18",
      "total_emission": 0.0,
      "unit": "kg CO2e"
    }
  ],
  "suggested_categories": [...],
  "validations": [...]
}
```

## Project Structure

```
├── app/
│   ├── api/
│   │   └── invoices.py        # API endpoints
│   ├── core/
│   │   ├── config.py          # Settings (env vars)
│   │   ├── database.py        # PostgreSQL operations
│   │   └── cloudinary_service.py
│   ├── schemas/
│   │   └── invoice.py         # Pydantic models
│   └── services/
│       ├── ocr.py             # PDF text extraction + Tesseract OCR
│       ├── llm.py             # OpenRouter LLM client
│       ├── pipeline.py        # Orchestrates OCR → LLM → Validate
│       ├── validators.py      # Total/line-item cross-checks
│       ├── fallback.py        # Regex fallback if LLM fails
│       ├── category_matcher.py # Fuzzy emission category matching
│       └── storage.py         # Temp file management
├── scripts/
│   ├── test_all_json.py       # Test all sample bills → JSON
│   └── test_emission.py       # Test emission mapping
├── sample-bills/              # Sample PDFs for testing
├── main.py                    # Entry point
└── pyproject.toml
```

## Testing

```bash
# Test all sample bills and save results as JSON
uv run python scripts/test_all_json.py

# Test emission mapping with a specific bill
uv run python scripts/test_emission.py
```
