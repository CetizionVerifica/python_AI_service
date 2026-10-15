# app/scope3 (python_AI_service) — Scope 3 AI layer

> Layer spec. Goes to `python_AI_service/app/scope3/CLAUDE.md`. Only S3-2 (factor sheet parsing) and S3-12 (EEIO sector suggestion) touch this repo.

## Rules
1. **Read and map only.** This service returns candidates (columns, factor types, sectors, GHG category numbers) with a confidence from 0 to 100 and the source cell or row. ESG-lite validates and stores them, and a person confirms anything below 80.
2. **Factor sheets (`excel_parser.py`, S3-2):** stop dropping sub-columns. Return one candidate factor per sub-column, with `factor_type` (`Total`/`Direct` → combustion or life_cycle per the sheet's own label, `WTT` → wtt, `kg CO2`/`kg CH4`/`kg N2O` → gas parts of the same factor). Keep full precision as a string. The 4-dp rounding warning goes, because ESG-lite widens the column.
3. **EEIO sector suggestion (`app/scope3/eeio.py`, S3-12):** given a spend line (description, supplier name, amount, currency), suggest up to 3 sectors from the EEIO list that ESG-lite sends, each with a confidence. Never call a model with more than the line's own text. Never guess a currency.
4. **Category hint (`category_matcher.py`):** may return `ghg_category` with a confidence when mapping a parent category name. ESG-lite treats it as a proposal (`category_ghg_map_proposal`), never as the stored value.
5. **Models:** the existing OpenRouter mapping provider (`OPENROUTER_MAPPING_MODEL`). No new provider and no new key.
6. **Logs:** ids and counts only, never cell text.
