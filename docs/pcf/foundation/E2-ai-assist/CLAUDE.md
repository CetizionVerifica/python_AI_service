# E2 · AI assist for PCF (python_AI_service)

> Blueprint spec. Repo **python_AI_service** (FastAPI). Follows the design rule in `../../../redesign/00-entity-map.md` §3: anything AI fills shows an "AI" chip and a confidence, is editable, and is never saved without the person confirming.

| Capability | Endpoint (proposed) | Builds on | Used in |
|---|---|---|---|
| **BOM import**: read an ERP export or spec sheet (xlsx/csv/pdf), return lines {material, qty, unit, per declared unit} | `POST /v1/pcf/parse-bom` | `excel.py` upload → preview flow, column inference | C02 step 2 "Import BOM" |
| **Material → factor matching**: suggest MaterialFactor ids for each line with confidence and reason ("EC-grade aluminium ingot, GCC → Primary aluminium, Middle East, IAI 2023") | `POST /v1/pcf/match-factors` | `CategorySuggestion` pattern | C02 step 2, C04 import |
| **Factor sheet import**: any layout of factor spreadsheets into MaterialFactor rows | `POST /v1/pcf/material-factors/parse-excel` | `emission_factors.py` parse-excel + re-analyze | C04 Import |
| **Supplier declaration reading** (phase 3): extract declared unit, kgCO₂e, boundary, standard, validity, verifier from a supplier EPD/PCF PDF; flag mismatched boundary | `POST /v1/pcf/parse-declaration` | `invoices.py` extraction pipeline | C06 |

## Rules
- Units are normalised with the same unit tables as invoice extraction; a unit the engine can't convert is returned as a warning, never guessed.
- Confidence < 60 shows in the warn tint; such lines block "Calculate" until confirmed.
- Store the raw uploaded file as evidence (same storage as `EmissionDocument`) and link it to the PcfStudy.

## Acceptance
- On 3 sample BOM files (supplied in phase 0) ≥ 90% of lines parsed with correct qty and unit.
- Every suggestion returns `reason` text the UI can show.

## Built (C06 1/5, phase 3)
- `POST /v1/pcf/parse-declaration` (`app/api/pcf.py`, `app/services/pcf_declaration.py`): multipart `file` (.pdf or PACT .json), `expected_boundary` (default `cradle_to_gate`), `expected_unit`, `study_id`, `keep_evidence`. Returns `fields` (product_name, supplier_name, declared_unit, value_kgco2e per declared unit, boundary, standard, gwp_set, reference_start/end, valid_from/to, verifier), each `{value, confidence, reason, by: pact|ai|rule}`, plus `boundary_mismatch`, `unit_mismatch`, `low_confidence_fields`, `warnings`, `problems`, `ai_used`, `evidence`.
- A PACT 2.x/3.x file is read without the AI (confidence 100 for fields the file states; 70 for PACT defaults such as the cradle-to-gate boundary and the three-year validity). A PDF goes through the invoice OCR path and the AI, capped at confidence 85; a failed AI read is a 422, never a guess.
- A per-tonne figure for a per-kg input (or the reverse) is converted and said so in the reason; any other unit difference is `unit_mismatch`. A boundary other than `expected_boundary` sets `boundary_mismatch` with a warning, so C06 can refuse to accept it silently.
- The raw file is kept in `pcf_evidence` with kind `declaration`.
