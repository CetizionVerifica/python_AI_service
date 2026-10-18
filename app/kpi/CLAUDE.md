# app/kpi (python_AI_service) — KPI suggestions layer

> Layer spec. Goes to `python_AI_service/app/kpi/CLAUDE.md`. Read it after the root CLAUDE.md, `docs/kpi/00-plan.md` and `01-contracts.md` (in ESG-lite), and before KPI-12's module CLAUDE.md.

## Shape
```
app/kpi/
  CLAUDE.md          this file
  router.py          POST /v1/kpi/suggest-values, POST /v1/kpi/suggest-mapping (service key only)
  values.py          document → candidate KPI values
  mapping.py         custom KPI → candidate library disclosures
  schemas.py         pydantic request/response models mirroring 01-contracts §3
ci/tests/test_kpi_*.py
```

## Rules
1. **Suggest only.**
   - Responses are candidates: `{kpi_code, value, unit, period_start, period_end, site_hint, source_page, quote, confidence}`.
   - The backend stores nothing until a person confirms in the entry card.
   - Never approve, never calculate a reported total, never convert a unit silently (return the unit as written).
2. **Only selected KPIs.** The backend sends the company's selected KPI codes with their questions and units. Suggestions outside that list are dropped.
3. **Scoped and private.**
   - Called only by ESG-lite with the service key and a `company_id`.
   - Documents are read from the existing upload storage by id.
   - Logs carry ids and counts, never document text or values.
4. **Plain confidence.**
   - `confidence` is 0–1, with a one-line `why` ("found 'Water consumed: 1,200 m³' on page 3").
   - Below 0.6, the FE shows the suggestion as "check this" only.
5. **Tests:**
   - fixtures for a water bill, an HR headcount summary and a waste manifest;
   - a check that suggestions outside the selected KPIs are dropped;
   - a service-key and company scoping check.

## As built: KPI-12 part 1/3, python (2026-10-18)
Files: `router.py`, `schemas.py`, `values.py`, `mapping.py`, plus `documents.py` (find, scope and read the stored file), `prompts.py` (both prompts) and `model.py` (the model call). Router mounted in `app/main.py` under `/v1`. Tests: `ci/tests/test_kpi_values.py`, `test_kpi_mapping.py`, `test_kpi_routes.py`; fixtures `ci/fixtures/kpi12/` (`water-bill.pdf`, `hr-summary.xlsx`, `waste-manifest.pdf`, each with the `*.model.json` answer the fake model replays; `make_fixtures.py` rebuilds them).

**`POST /v1/kpi/suggest-values`** (service key only; a user token is 403, no or a wrong key 401)
- In: `{company_id, document_id, kpis: [{code, question, unit?, allowed_units?, breakdowns?}], period_hint?, user_id?}`. `kpis` 1–200 with unique codes. `breakdowns` is KPI-2's `{by, values}` (or a plain list of keys). `period_hint` is `{period_start?, period_end?, frequency?}` (ISO dates) or a short text ("FY 2025-26"); it is only a hint in the prompt. `user_id` is optional: when sent, the document must have been uploaded by that user.
- Out: `{document_id, document_name, pages, candidates: [{kpi_code, value, unit, breakdown_key, period_start, period_end, site_hint, source_page, quote, confidence, why}]}`. `value` is a number (a string only for a non-numeric answer, e.g. "Yes"); `unit` is one of the KPI's `allowed_units` (spelled as ESG-lite sent it), else its `unit`, else as written, or null; `breakdown_key` is null for the KPI as a whole or one of its breakdown keys; periods are ISO dates or null; `source_page` is 1-based (the sheet number for a spreadsheet); `confidence` 0–1 with a one-line `why`. Up to 5 per KPI, best first, in the request's KPI order. `document_name` is for the "Suggested from water-bill.pdf, page 2" label.
- The document: `uploaded_documents.id`. It belongs to the company of the user who uploaded it (`uploaded_by` → that user's `site_id` and `user_sites` → `site.company_id`). Missing, another company's, uploaded by someone other than `user_id`, without an uploader, uploaded by a Superadmin, or already imported/deleted → 404 (one answer for all). File gone → 410, unsupported or unreadable → 422, storage down → 502. Read as pages: PDF text per page (scanned PDFs and images through Tesseract, like bills), `.xlsx` one page per sheet, `.csv`/`.txt` one page; at most `MAX_PAGES` pages, 12,000 characters a page, 60,000 in all.
- Checks on every model candidate (dropped, never repaired): the code is one of the request's (exact match); the breakdown key is the KPI's; the unit is an allowed one by spelling only (`m³` = `m3`, `tonnes` = `t`, a bare count takes a count unit); anything else would be a conversion, so it is dropped; the quote is on the cited page (else the page that has it; else dropped); a number appears in its own quote, so a sum, average or converted figure can't come back; a 0–100 confidence is scaled to 0–1; a period that isn't an ISO date, or ends before it starts, is null; duplicates go.
- No model configured, or the call fails or answers non-JSON → `503 {"detail": "AI not available right now"}` (the proxy's degrade). A document with no text → 200 with no candidates.

**`POST /v1/kpi/suggest-mapping`** (service key only)
- In: `{name, question?, unit?, topic?, catalogue: [{framework, code, title, section?}], company_id?}`; catalogue 1–5,000 rows, `(framework, code)` unique (the same code may sit under two frameworks). `company_id` is only logged.
- Out: `{suggestions: [{framework, code, title, section, why, confidence}]}`, at most 5, best first. Title and section are the catalogue's. A `(framework, code)` the catalogue doesn't hold is dropped, so there is no free-form framework knowledge.
- A word-overlap shortlist of 60 goes to the model. Without a model, or when it fails, the shortlist's best 5 sharing a word come back with confidence at most 0.5 (shown as "Check this").

**`POST /v1/kpi/documents`** (signed-in user; added so the drop zone has somewhere to put a PDF: `/v1/excel/upload` takes only sheets)
- Multipart `file` (PDF, image, `.xlsx`, `.csv`, `.txt`; at most `MAX_FILE_MB`), stored like the bulk upload (Cloudinary, or the local temp path without it) in `uploaded_documents` with `uploaded_by` = the caller → `{document_id, document_name}`. A Superadmin gets 403 (a Superadmin's upload has no company). The service key gets 403.

**Model**: the OpenRouter mapping model (`OPENROUTER_MAPPING_MODEL` through `llm.call_openrouter_within`, budget `MAPPING_MODEL_TIMEOUT_S`), used when `MAPPING_MODEL_PROVIDER` is `openrouter`, or `auto` with the OpenRouter key, as S3-12 does. Logs: one line per request with ids and counts (pages, KPIs, candidates, drops by reason), never document text, values or KPI wording.
