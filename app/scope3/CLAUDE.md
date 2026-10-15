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
- `factor_type` from the sheet's own labels: WTT → `wtt`; a Total next to a WTT sub-column → `life_cycle` with `includes_wtt: true` (D6, S3-7 flags it); life cycle / WTW in the sub-column or sheet title → `life_cycle`; otherwise `combustion`. A one-value sheet only gets a type when its title says WTT or life cycle (else null).
- Gas columns (`kg CO2`, `kg CH4`, `kg N2O`, biogenic / "Outside of scopes") are parts (`co2_kg`, `ch4_kg`, `n2o_kg`, `biogenic_co2_kg`) of the Direct candidate, or of the primary when there is no Direct column. "kg CO2e" is a factor, not a gas part. A CH4 or N2O share given in CO2e ("kg CO2e of CH4", DEFRA's layout) is left out with one warning per column, because the contract keeps those gases in kg of the gas; "kg CO2e of CO2" is kg of CO2.
- `factor_value_text` is the exact factor as a plain decimal string (float cells read at 12 significant digits to drop Excel noise). `factor_value` stays a float for older readers. The 4-dp rounding and its warning are gone: merge after ESG-lite's S3-2 part widens `factor_value` to numeric(18,8).
