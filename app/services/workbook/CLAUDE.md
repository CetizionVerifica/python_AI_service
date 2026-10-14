# XI1 · Workbook reader (python_AI_service)

> Goes to `python_AI_service/app/services/workbook/CLAUDE.md`. Depends on XI0. Blocks XI2. Lane L1.

## Outcome
Any .xlsx/.xls/.csv becomes a deterministic, model-free description: every sheet, its header row, cleaned data rows, candidate columns and a fingerprint. Messy layouts stop being the model's problem.

## Build
- `app/services/workbook/reader.py`: `read_workbook(path) -> Workbook` with `sheets: Sheet[]`; each `Sheet` has `name`, `header_row`, `header_depth` (multi-row headers joined with " / "), `columns: Column[]` (name, dtype guess, distinct count, sample values, fill ratio, numeric ratio, unit tokens found in header or cells like "(L)", "kWh"), `rows` (merged cells forward-filled, subtotal/total/blank/"grand total" rows dropped with their row numbers kept in `dropped_rows`), `layout` ("rows" or "months_as_columns" when ≥ 6 header cells parse as months), `fingerprint` (01-contracts §1), and `row_refs` (`Sheet!A12` for every kept row).
- Header detection per sheet: the first row where ≥ 60 % of non-empty cells are text and the next row has a different dtype profile; falls back to row 1. Reuses the existing header logic from `excel_parser` where it fits, then replaces it.
- Hidden sheets and sheets with < 2 data rows are listed with `skipped_reason`.
- Nothing here calls ESG-lite or an LLM.

## Own tests
`ci/tests/test_workbook_reader.py` on a fixture set of 12 layouts (single sheet monthly; electricity bills with a date column; multi-site one sheet; one sheet per category; yearly CY; yearly FY; months as columns; mixed units; two-row merged header; subtotal rows; a hostile cell; 15,000-row ledger). Fingerprint stable across sheet rename and row count; identical headers in a different order give the same fingerprint.

## Acceptance
Every fixture produces the expected `Sheet` description (golden JSON per fixture, checked in).

## As built (2026-10-14, `[XI1 1/1]`)

Files: `app/services/workbook/reader.py` (+ `__init__.py`), fixtures `ci/fixtures/xi1/` (12 workbooks built by `make_fixtures.py`, golden descriptions under `golden/`), tests `ci/tests/test_workbook_reader.py` (`UPDATE_GOLDEN=1` refreshes the golden JSON after an intended change).

- `read_workbook(path)` / `read_workbook_bytes(content, ext, name)` → `Workbook { name, ext, sheets: Sheet[] }`. `.xlsx` through openpyxl (`data_only=True`, so a formula reads as its cached value or blank, never as the formula), `.csv` through the csv module (delimiter sniffed, BOM stripped, one sheet named after the file), `.xls` through pandas (needs `xlrd`, which is not a dependency: the error says to save as .xlsx). At most 20 sheets (`MAPPING_SHEETS_PER_WORKBOOK`); the rest are listed with `skipped_reason`.
- `Sheet`: `name`, `index`, `header_row` (1-based), `header_depth`, `columns: Column[]`, `rows` (`{row, row_ref: "Sheet!A12", cells: {column name: text}}`), `dropped_rows` (`{row, reason: blank | total | legend}`), `layout` (`rows` | `months_as_columns` when ≥ 6 header cells parse as months), `fingerprint`, `data_rows`, `skipped_reason` (`hidden`, `empty`, `fewer than 2 data rows`, `more than 20 sheets`), `hidden`. `describe()` is the sheet without its rows (what the golden files and, later, the prompt builder use); `sample_rows(n)` the first n rows' cells.
- `Column`: `name` (multi-row headers joined with " / ", blank headers `Column C`, duplicates `Qty (2)`), `letter`, `dtype` (`text` | `number` | `date` | `mixed` | `empty`, ≥ 80 % of filled cells decides), `distinct`, `samples` (5 distinct values in sheet order), `fill_ratio`, `numeric_ratio`, `unit_tokens` (from the header: "(L)", "[kWh]", "in INR", or a header that is only a unit such as "kWh"; and from cells like "1,200 litre"), `month` (`{month, year|null, date|null}` when the header names a month: "Jan", "Jan-24", "January 2025", "2025-01", "03/2025", a date).
- Header detection (`detect_header`): the first of the top 20 rows with ≥ 2 filled cells, ≥ 60 % text among them and no number, followed within three rows by a row with a different dtype profile; falls back to row 1. The header grows downward (max depth 3) while the next row is header-like and the row above it has repeated or missing names, which is what a merged two-row header looks like after merged cells are forward-filled with their top-left value.
- Rows dropped: all-empty rows; total rows (see "Review fixes" below for the exact rule); column-letter legend rows ("A | B | C"). Nothing else is dropped: a row with a blank value stays, so XI2 can flag it.
- Cell text: whole floats without ".0", dates as ISO (date only when midnight), everything else `str()` with whitespace collapsed. Cells are never parsed further here and never logged.
- Fingerprint: sha256 of the sorted, normalised (lower case, punctuation out, spaces collapsed) header names plus `#depth=N`. Sheet name, row count and column order play no part; one header more or a different depth changes it.
- Not done here: the legacy P27 contributor path (`excel_parser.detect_header_row`, `read_headers_from_bytes`, `map_df`) is untouched; XI2/XI4 switch the smart flow onto this reader and the contributor path is retired with it, not in this PR.

### Review fixes (2026-10-14)
- A sheet is sized by its last cell that holds a value, never by formatting, and is read at most 512 columns wide (`MAX_COLUMNS`); a sheet formatted out to `XFD` reads at once.
- Total rows: the label must be the total word alone or followed by punctuation, a bracket, a number or tag ("Q1", "FY25") or for / of / YTD ("Total (kWh)", "Sub total Q1", "Total for FY25"). "Total Energies" is a name and is kept. Every text cell before the first number is checked, so a "Subtotal" under a merged, filled-down site name is dropped.
- A row whose filled cells are all the same text (a merged title) is never the header.
- Next to at least one text cell, two or more year numbers ("Supplier | 2023 | 2024") or six or more month dates count as header cells, so year and date headings are a header.
- Text dates are day first: "01/03/2025" is March 2025 ("2025/03/01" is year first), so a CSV pivot with date headings is `months_as_columns`.
- A damaged .xlsx is a plain `ValueError` with a message for the user.
- Chart sheets are listed (`skipped_reason: "chart sheet"`), so every sheet keeps its index in the workbook.
- **Totals labelled at the end (audit, 2026-10-14).** A text cell before the first number that ends with "total" or "subtotal" ("Q1 subtotal", "Plant A Total", "Site sub-total:") is a total too. A name that only starts with the word ("Total Energies") is still data. XI6 case 09 reads 6 data rows.
