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
