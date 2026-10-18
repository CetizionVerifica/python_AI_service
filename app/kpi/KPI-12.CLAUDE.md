# KPI-12 · AI assist: values from documents, mapping suggestions (suggest only)

> Module spec. Lane L5. Copy to `python_AI_service/app/kpi/KPI-12.CLAUDE.md`, `ESG-lite/src/kpi/ai/CLAUDE.md` and `ESG-lite_FE/src/features/kpi/my-kpis/KPI-12.CLAUDE.md`. PR titles `[KPI-12 k/m]` (1/3 python, 2/3 ESG-lite proxy, 3/3 FE chips).
> Depends on: KPI-4, KPI-7, KPI-2 · Owns: python `app/kpi/*`, ESG-lite `src/kpi/ai/*`, FE suggestion chips in `KpiQuestionCard` and the custom-KPI drawer · Touches: —

## Job to be done
Two jobs, with a person always confirming:
- A contributor drops a water bill, an HR summary or a waste manifest, and the matching KPI cards fill in with a suggested value, unit and period, labelled "Suggested from water-bill.pdf, page 2".
- An Admin adding a custom KPI gets suggested framework disclosures to map it to.

## Today
- **The AI service** already reads documents for emission bills (`/v1/bills`, `uploaded_documents`) and maps Excel (`/v1/excel`) and Scope 3 sectors (`/v1/scope3/eeio-sector`), with a service key. Rule: read and map only, never calculate.
- **The FE** shows AI fields with `AiLabel` / `isAiField` in add-data.

## Build
### AI service (python layer file)
1. **`POST /v1/kpi/suggest-values`** `{company_id, document_id, kpis:[{code, question, unit, allowed_units, breakdowns}], period_hint}` returns candidates per the layer file. Candidates for KPIs outside the request list are dropped. Tests use fixtures.
2. **`POST /v1/kpi/suggest-mapping`** `{name, question, unit, topic, catalogue:[{framework, code, title, section}]}` returns the top 5 disclosures with a `why` and a confidence. It uses the catalogue passed in; there's no free-form framework knowledge.

### Backend
1. `POST /kpi/ai/suggest-values` (an owner of the KPIs on that site): checks scope, builds the request from the caller's due KPIs, calls python, and returns the suggestions. Nothing is stored except an audit-free cache keyed by document id for 24 h.
2. `POST /kpi/ai/suggest-mapping` (Admin): passes the company's enabled catalogue.
3. Both answer `KPI_TRACKING_OFF` when the switch is off, and degrade to "AI not available right now", which isn't an error, when python is down.

### Frontend
1. **My KPIs:** a "Fill from a document" drop zone in the header.
   - Suggestions appear on the matching cards as an "AI suggestion" chip (value, unit, source page, quote on hover) with Use / Dismiss.
   - "Use" fills the input only; the person still saves and submits. Below 0.6 confidence the chip reads "Check this".
2. **Custom-KPI drawer** (KPI-3): suggested disclosures as checkboxes with their `why`.

## Tests
- **python:** fixtures for a water bill, an HR summary and a waste manifest; suggestions limited to the requested KPIs; service key and company scoping.
- **API:** scope (another site's KPI is refused); switch off; AI down degrades gracefully.
- **Playwright:** drop a document, then a suggestion appears, then Use, then the value is in the draft, not submitted.

## Out of scope
Automatic approval. Calculating totals. Reading documents the caller can't access.

## Done when
Contributors can fill KPI cards from their documents and Admins get mapping suggestions. Every suggestion is labelled, sourced and confirmed by a person.
