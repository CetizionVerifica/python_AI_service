# app/scope3 (python_AI_service) — Scope 3 AI layer

> Layer spec. Goes to `python_AI_service/app/scope3/CLAUDE.md`. Only S3-2 (factor sheet parsing) and S3-12 (EEIO sector suggestion) touch this repo.

## Rules
1. **Read and map only.** This service returns candidates (columns, factor types, sectors, GHG category numbers) with a confidence from 0 to 100 and the source cell or row. ESG-lite validates and stores them, and a person confirms anything below 80.
2. **Factor sheets (`excel_parser.py`, S3-2):** stop dropping sub-columns. Return one candidate factor per sub-column, with `factor_type` (`Total`/`Direct` → combustion or life_cycle per the sheet's own label, `WTT` → wtt, `kg CO2`/`kg CH4`/`kg N2O` → gas parts of the same factor). Keep full precision as a string. The 4-dp rounding warning goes, because ESG-lite widens the column.
3. **EEIO sector suggestion (`app/scope3/eeio.py`, S3-12):** given a spend line (description, supplier name, amount, currency), suggest up to 3 sectors from the EEIO list that ESG-lite sends, each with a confidence. Never call a model with more than the line's own text. Never guess a currency.
4. **Category hint (`category_matcher.py`):** may return `ghg_category` with a confidence when mapping a parent category name. ESG-lite treats it as a proposal (`category_ghg_map_proposal`), never as the stored value.
5. **Models:** the existing OpenRouter mapping provider (`OPENROUTER_MAPPING_MODEL`). No new provider and no new key.
6. **Logs:** ids and counts only, never cell text.

## As built: S3-2 factor sheets (2026-10-15)
- `excel_parser._extract_factors` returns one `EmissionFactorRecord` per sub-column (Total, Direct, WTT…). The schema's primary sub-column keeps the plain name and `primary: true`; the others are named `<name> - <sub-column>`, so older imports that only read the primary keep their names.
- `factor_type` from the sheet's own labels: WTT → `wtt`; a Total in a row whose WTT cell has a number → `life_cycle` with `includes_wtt: true` (D6, S3-7 flags it); life cycle / WTW in the sub-column or sheet title → `life_cycle`; otherwise `combustion`. A one-value sheet only gets a type when its title says WTT or life cycle (else null).
- Gas columns (`kg CO2`, `kg CH4`, `kg N2O`, biogenic / "Outside of scopes") are parts (`co2_kg`, `ch4_kg`, `n2o_kg`, `biogenic_co2_kg`) of the Direct candidate, or of the primary when there is no Direct column. "kg CO2e" is a factor, not a gas part. A CH4 or N2O share given in CO2e ("kg CO2e of CH4", DEFRA's layout) is left out with one warning per column, because the contract keeps those gases in kg of the gas; "kg CO2e of CO2" is kg of CO2.
- `factor_value_text` is the exact factor as a plain decimal string (float cells read at 12 significant digits to drop Excel noise). `factor_value` stays a float for older readers. The 4-dp rounding is gone (ESG-lite's S3-2 part widens `factor_value` to numeric(18,8)). Factors and gas parts past 8 decimal places are rounded half-up to 8, with one warning per column, because ESG-lite skips a row whose value has more. A negative gas part is left out with a warning.
- Factor types, `includes_wtt` and gas-part assignments are read deterministically from the sheet's own column labels and titles, never by the model, so they carry no confidence score (layer rule 1 applies to model guesses). The source is the candidate's `sub_column` and the sheet; a person confirms them in the import Preview.

## S3-12 · Screening, relevance, proxy and extrapolation (module spec, working copy)


> Module spec. Lane L3. Copy to `ESG-lite/src/scope3/screening/CLAUDE.md`, `ESG-lite_FE/src/features/scope3/screening/CLAUDE.md` and `python_AI_service/app/scope3/CLAUDE.md` (append a "S3-12" section) as the first commit. PR titles `[S3-12 k/m] …` (1/3 ESG-lite, 2/3 python_AI_service, 3/3 ESG-lite_FE).
> Depends on (merged): S3-1, S3-5 · Owns: `src/scope3/screening/*`, FE `features/scope3/screening/*`, `Emission.proxy` · Touches: `company_scope3_category.status` (write via S3-1 service)

### Job to be done
An Admin sees a rough size for all 15 categories in one screen (from what is recorded plus cheap spend or headcount estimates), ticks why each matters, accepts or overrides a suggested Included / Excluded / Not relevant status, and fills known gaps with clearly labelled extrapolated rows.

### Guidance (what the GHG Protocol expects)
- Screen all 15 categories first with less specific data, then refine the largest and most relevant (L1122-1150). Rank with Table II: size, influence, risk, stakeholders, outsourcing, sector guidance, spending or revenue analysis, other (L1149-1173).
- Choose methods per activity by size, business goals, data availability, quality and cost; methods can be mixed within a category (L1100-1120, L1176-1188).
- EEIO (spend-based) data is suited to screening; it is sector-average and uncertain (L1340-1360). Investments should be screened with average data first (L6161-6168).
- Proxy data may fill gaps, extrapolated or scaled up, e.g. 80 of 100 facilities via an intensity per unit of output (L1384-1395); sampling per Appendix A (L1396-1399, L6684-6760). Coffee example: data for suppliers covering 85 % of volume extrapolated to 100 % (L1189-1196).
- Proxy and extrapolated data are secondary data (L1289-1291) and must be described in the report (L1382-1383).

### Today (evidence)
- No screening view, relevance criteria or estimate exists (gap report §4.2).
- Sites without data are excluded, not estimated (`ESG-lite/src/reporting/ghg.ts:447`).
- `company_scope3_category.relevance` (jsonb) and `status` arrive with S3-1; spend FX, deflator and USEEIO lookups arrive with S3-5 (`src/scope3/spend/*`).
- No proxy method exists; `compute.ts` has one formula (`ESG-lite/src/services/emissionWrite/compute.ts:44-45`). After S3-0, `methods/proxy.ts` is a stub that falls back to `activity`.

### Build
#### Backend (ESG-lite)
1. `migrate:scope3-screening`: `emission.proxy jsonb null` (`{basis, coverage_pct, from}`, §4). S3-0 follow-up: table `scope3_screening_input` (company_id, year, ghg_category, basis `spend|headcount|revenue|mass_sold|other`, amount, currency, eeio_sector, note, updated_by/at; unique per company, year, category, basis), so categories with no rows can still be estimated.
2. `screening/service.ts` → `GET /scope3/screening?year=&year_type=`: 15 rows `{n, status, reason, recorded_tco2e, estimate_tco2e, basis, share_pct, relevance, suggestion: {status, reason}}`.
   - `recorded_tco2e`: Σ `total_kgco2e` / 1000 of approved and pending rows in the year (pending shown separately).
   - `estimate_tco2e`: screening inputs × S3-5 lookups (FX at mid-year, deflator, USEEIO sector factor), or headcount × the Cat 7 average form defaults. Estimates are computed here and never saved as emission rows (layer rule 1 exception).
   - Suggestion rules (`screening/rules.ts`, constants, unit tested; the guidance sets no numeric threshold, so the UI calls them suggestions): categories that make up the top 95 % of the estimated total, or have any Table II tick, → `included`; estimate < 1 % with no tick → `excluded`, reason "Estimated below 1 % of Scope 3 (≈ x tCO₂e by spend screening, 2026)"; no input, no rows, no tick → `not_relevant`, reason "No activity in this category in 2026".
3. `PUT /scope3/screening/inputs` (S3-0 follow-up: add to §5) for the inputs; relevance ticks and "Apply suggestion" go through the S3-1 boundary service (`PUT /scope3/categories/:n`), Admin and Superadmin only (D12), with the suggestion's reason prefilled and editable.
4. `methods/proxy.ts` (owned here) and `POST /scope3/extrapolate`: body `{ghg_category, site_id, year, year_type, basis: "coverage" | "intensity", coverage_pct?, source_filter?, output_site_ids?}` (S3-0 follow-up: fix the body shape in §5).
   - coverage: proxy total = Σ source kg × (100 − coverage_pct) / coverage_pct.
   - intensity: (Σ source kg ÷ Σ approved production at source sites) × production at the sites without data (`ProductionData`).
   - Sources are approved rows of that category and year; proxy rows are never sources. The row is Pending, `method` = most common source method, `data_type secondary`, `is_estimate true`, `proxy = {basis, coverage_pct, from: [ids]}`; S3-3 then scores completeness 3.
   - A second pending proxy for the same category, site, year and basis → row issue `DUPLICATE_EXISTING` with the existing id. S3-0 follow-up: add error `EXTRAPOLATION_BASIS_INVALID` (coverage outside 1-99, no source rows, no production).
5. Backfill S3-9's `extra_data.survey` into `emission.proxy` with basis `sample` (S3-9 step 5). Roles: Manager+ may extrapolate for own sites; read for any role with site access. Audit rows (`scope3_proxy`).

#### Frontend (ESG-lite_FE)
1. `src/features/scope3/screening/` (route `/settings/scope3/screening`, Admin and Superadmin edit, Manager read-only; `?year=` in the URL): `PageHeader` "Scope 3 screening", `KpiStrip` (recorded tCO₂e, estimated tCO₂e, included categories n/15, categories with a reason n/15), then a `DataTable` of 15 rows: `Scope3CategoryChip`, recorded, estimate with basis ("Estimate, not saved"), share bar with the number, 8 relevance checkboxes (Table II wording in tooltips), suggestion, current `StatusPill`, "Apply". Row drawer: inputs (basis, amount, currency, sector with "Suggest sector"), and "Extrapolate…" dialog (coverage % or intensity; shows the formula and the sources before creating a Pending row).
2. Proxy rows show an "Extrapolated" badge plus basis in the approvals and my-entries drawers via the shared `src/ui/DataQualityBadge` tooltip (S3-3 owns the badge; data-only change).
3. Loading, empty ("Add a spend figure to estimate this category"), error states; keyboard reachable ticks; 390 px shows a stacked card per category.

#### AI service
1. `app/scope3/eeio.py` and `POST /v1/scope3/eeio-sector` (S3-0 follow-up: name the route): input `{lines: [{description, supplier, amount, currency}], sectors: [{code, name}]}` from ESG-lite; output up to 3 `{code, confidence 0-100}` per line. Only the line's own text goes to the model (`OPENROUTER_MAPPING_MODEL`); never a currency guess; logs ids and counts only. ESG-lite asks for confirmation below 80.
2. `category_matcher.py` `ghg_category` hint, if S3-1 did not add it.

### Golden cases
No guidance example has numbers for screening. Unit cases (S3-0 follow-up: add as `intro-proxy`, constructed from L1189-1196): known 850,000 kg at 85 % coverage → proxy **150,000** kg; intensity: 2 sites 1,000 t CO₂e for 500 t output, missing site 250 t output → proxy **500,000** kg.

### Tests
- API (`ci/api/tests/s3-12-screening.test.cjs`): company 2 token on company 1 screening, inputs and extrapolate → 404; Manager apply-suggestion → 403; proxy row never used as a source; duplicate proxy → `DUPLICATE_EXISTING`; estimate never creates an emission row (count before and after).
- Unit: `rules.ts`, coverage and intensity maths in `proxy.ts`; AI: `test_eeio.py` (prompt holds only the line text; low confidence returned as is).
- Playwright (`e2e/scope3-screening.spec.ts`): enter Cat 6 spend, see the estimate, tick "Stakeholders", apply "Included".

### Out of scope
Uncertainty ranges (Appendix B). Choosing a sample. Any automatic status change without an Admin click.

### Done when
- All 15 categories show recorded and estimated sizes for a year, with relevance ticks stored.
- Suggestions can be applied with an editable reason through the boundary service.
- Extrapolated rows are Pending, secondary, estimated, and carry their basis.
- `ci/golden` unchanged.

## As built: S3-12 EEIO sector suggestion (2026-10-16)
- `app/scope3/eeio.py` and `POST /v1/scope3/eeio-sector` (`app/api/scope3.py`): request and response exactly as contracts §5. ESG-lite's service key only (a user token gets 403); ESG-lite calls it from `POST /scope3/screening/sector-suggestions` with its USEEIO library as the sector list.
- Per line: a deterministic shortlist of 40 sectors by word overlap with the line (description and supplier; the "(NAICS …)" suffix ignored), then one mapping-model call (`OPENROUTER_MAPPING_MODEL` through `llm.call_openrouter_within`, used when `MAPPING_MODEL_PROVIDER` is `openrouter`, or `auto` with the OpenRouter key) that picks up to 3 codes from the shortlist with a confidence. The prompt holds the line's description and supplier and the shortlisted codes and names only: no other line, no amount, and no currency question.
- Codes outside the shortlist and duplicates are dropped, confidences are clamped to 0–100 and otherwise returned as the model gave them (ESG-lite asks for confirmation below 80). Without a model, or when the call fails or answers in another shape, the shortlist's best 3 come back with the word-match score scaled to at most 60, so a person always confirms them.
- Logs: one line per request with line, sector and model-call counts.
- Not built: the `category_matcher.py` `ghg_category` hint (spec AI item 2, "if S3-1 did not add it"). S3-1 proposes GHG numbers in ESG-lite (`src/scope3/taxonomy/propose.ts`) and nothing would call an AI hint, so it is left for a module that needs it.
- Tests: `ci/tests/test_eeio.py`.
