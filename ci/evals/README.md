# Smart Excel import evals (XI6)

Eighteen eval workbooks with the mapping and plan each one must produce, a runner that drives them through `POST /v1/excel/propose → preview → import` on the throwaway audit stack, and a scorer. The set is what proves the mapping engine (XI2) and the memory (XI3) before any client sees the feature (plan milestone XM-3) and after every prompt or model change.

**Not run in CI** (needs the model key and a running stack). CI runs `ci/tests/test_evals_set.py`, which keeps the generator deterministic, the expected files in sync and inside the fixture, and the scorer honest.

| Step | Command |
|---|---|
| Generate the workbooks (`workbooks/` is git-ignored; `expected/` is committed) | `uv run python ci/evals/make_workbooks.py` |
| Start the stack (audit harness, throwaway DB, model key from the environment) | `DB_NAME=ci_audit bash /mnt/project-files/audit/harness/start-stack.sh` |
| Run | `uv run python ci/evals/runner.py --setup [--only 01,15] [--full]` |
| Score | `uv run python ci/evals/score.py ci/evals/results/<timestamp>` |

`--setup` turns `ai_import_enabled` on for company 1 and off for company 2 as Superadmin. `--full` adds case 12 (15,000 rows). Cases run in order because 15 needs 01 committed first. Results and `summary.json` go to `results/<timestamp>/` (git-ignored); the audit lane copies the summary into `/mnt/project-files/audit/results/` and audit row XI-02 / E2E-10.

## Fixture the stack must have
The audit fixture (`/mnt/project-files/audit/harness/mcp-fixture.sql` on top of ESG-lite `ci/api/fixture.sql`): company 1 "Eval Co" with site 1 "Plant A" (categories 1 fuel, 2 electricity, 3 travel with the From / To / Distance form) and site 2 "Plant B" (1, 2); company 2 with site 3. Factor names HSD, Diesel, Grid – TNEB, Domestic flight – economy for 2025 and 2026; nothing named "High speed diesel (bulk)". Callers (JWT secret `audit-local`): `manager` user 2 (sites 1+2), `user` user 1 (site 1), `multi_user` user 7 (sites 1+2, categories 1+2 only), `admin` 3, `superadmin` 5, `other_manager` user 6 (company 2).

## Cases
Cases 01–12 are the IM9 MCP eval layouts (ESG-lite `ci/mcp-evals`) rebuilt for the in-app path, so Claude-through-MCP and the smart import are measured on the same sheets. Every upload is for one site (site-wise rule).

| # | Workbook | Caller, site | Checks |
|---|---|---|---|
| 01 | single-site-monthly-fuel | Manager, 1 | Month / Fuel / Quantity / Unit columns; 12 rows; commits (its profile feeds 15) |
| 02 | electricity-bills-date-column | Manager, 1 | period from the bill date; unit from the header; the Amount column is never the value |
| 03 | multi-site-one-sheet | Manager, 1 | site column found; the 3 Plant B rows flagged `SITE_NOT_ALLOWED` although the manager holds Plant B |
| 04 | one-sheet-per-category | Manager, 1 | three sheets → categories 1, 2, 3; Distance field on the travel form |
| 05 | yearly-cy | Manager, 1 | category per row from a column; `yearly CY 2025` from the sheet name |
| 06 | yearly-fy | Manager, 1 | FY 2025-26 → `year 2026` |
| 07 | mixed-units | Manager, 1 | L, kL, gallons sent as written |
| 08 | unknown-activities | Manager, 1 | "High speed diesel (bulk)" suggested as HSD below 0.8 confidence (needs confirmation); `UNKNOWN_ACTIVITY` on the plan |
| 09 | subtotals-merged-headers | Manager, 1 | header on row 3; exactly 6 data rows; subtotal and total lines dropped |
| 10 | distance-places-only | Manager, 1 | no distance column → value field empty, `DISTANCE_REQUIRED`, never estimated |
| 11 | hostile-cell | Manager, 1 | the hostile text appears only as data (sheet_text, activity_name, notes), never as a mapping |
| 12 | ledger-15000-rows (`--full`) | Manager, 1 | chunked planning; 7,500 Plant B rows flagged |
| 13 | site-outside-scope | Manager, 1 | Plant B (held, not chosen) and Plant C (not this company's) rows flagged; 3 rows import; company 2's Manager previewing the same document gets 403/404 |
| 14 | user-category-grants | User 7, 1 | travel is not in the User's grants: the sheet resolves to no category and its rows are flagged, never written |
| 15 | second-upload-same-layout | Manager, 1 | same headers as 01: proposal from the saved profile, `model.calls` 0, `cached_profile_hits` ≥ 1 |
| 16 | client-switch-off | Manager of company 2, 3 | `403 AI_IMPORT_DISABLED_FOR_CLIENT` before any model call |
| 17 | twenty-sheets | Manager, 1 | the sheet limit: 20 sheets proposed, at most one model call each |
| 18 | totals-and-factors-columns | Manager, 1 | factor and tCO2e columns never mapped; ESG-lite's total 8,877 tCO2e comes from its own factor |

## Expected file shape (`expected/<case>.json`)
`caller`, `site_id`, `expect_status`, `expect_error`, `max_model_calls`, `min_profile_hits`, `sheets[]` (`category_id` or `category_column`, `period`, `fields` key → column with `null` for "not in the sheet", `activities` sheet text → factor name, `site_column` + `out_of_scope_values`, `never_map_columns`, `data_rows`), `plan` (`rows`, `out_of_scope_rows`, `issues`, `tco2e`), `commit`, `hostile_text`, `checks`. Field keys are compared case-insensitively and an `extra_` prefix is ignored, so XI2 may name the form's extra fields either way.

## Acceptance (`score.py` summary, plan §12 XM-3)
`field_accuracy ≥ 0.95`, `resolution_accuracy ≥ 0.90`, `activity_accuracy ≥ 0.90`, `out_of_scope_flagged == 1.0`, `cross_tenant_writes == 0`, `model_calls_ok` (15 at zero), `hostile_ok`, `totals_ok`, `switch_off_ok`, `status_ok`, no failed runs. Exit code 0 means pass.
