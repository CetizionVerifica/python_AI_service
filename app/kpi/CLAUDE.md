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
