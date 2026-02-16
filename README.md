# ocr-invoice

Minimal FastAPI service to extract fields from bills/invoices (PDF or images).

## Run

- Install deps: `uv sync`
- Start server: `uv run python main.py`

Health: `GET /health`

Extract: `POST /v1/extract` (multipart form-data)

Example:

```bash
curl -sS -X POST http://localhost:8000/v1/extract \
  -F "file=@/path/to/invoice.pdf" \
  -F "fields=invoice_number,invoice_date,vendor_name,subtotal,tax,total,currency" | jq
```

## LLM config

Set:
- `LLM_API_KEY` (or `OPENAI_API_KEY`)
- `LLM_MODEL` (default: `gpt-4o-mini`)
- `LLM_BASE_URL` (default: `https://api.openai.com/v1`)

Optional:
- `MAX_PAGES` (default: 3)
- `MAX_FILE_MB` (default: 20)
- `MIN_PDF_TEXT_CHARS` (default: 200)
- `MAX_CONCURRENT_REQUESTS` (default: 4)
- `MAX_CONCURRENT_LLM` (default: 2)
