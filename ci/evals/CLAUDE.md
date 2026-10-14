# XI6 · Evals and audit (project files + python_AI_service `ci/evals`)

> Goes to `python_AI_service/ci/evals/CLAUDE.md` and `audit/e2e-workflow-audit.md` §13. Depends on XI0 to write; runs as modules merge. Audit lane.

## Outcome
Mapping quality and tenant safety are measured, not assumed, before any client sees the feature and after every prompt or model change.

## Build
- Eval set `ci/evals/workbooks/`: IM9's 12 workbooks plus manager-scope cases: a sheet naming a site outside the manager's scope; a User with category grants; a second upload of the same layout (expects a profile hit and zero model calls); a client switch off; a 20-sheet workbook; a workbook with totals and factors columns (must be ignored).
- Expected `MappingProposal` and planned-row counts per workbook (`expected/*.json`). Scorer `score.py`: field mapping accuracy, site/category resolution accuracy, activity match accuracy, out-of-scope rows flagged, model calls, hostile cell inert. Thresholds: ≥ 95 % fields, 100 % out-of-scope flagged, 0 cross-tenant writes.
- Runner drives `POST /v1/excel/propose` → `preview` → `import` against the throwaway stack from `audit/harness/start-stack.sh` with Manager, User, Admin and Superadmin tokens from `mcp-fixture.sql`; needs the model key as a cloud-environment secret (same as IM9 option A).
- Audit harness: `xi-probes.cjs` with rows XI-01..XI-08 (from the module specs) added to `audit/e2e-workflow-audit.md` §13; hourly audit run picks them up like `mcp-probes.cjs`.

## Acceptance
Thresholds met on the full set before XM-3; results recorded in `audit/results/`.

## As built (2026-10-14, python_AI_service `ci/evals/`, audit lane)
- `make_workbooks.py` writes 18 workbooks (12 IM9 layouts + 13 site outside scope, 14 User with category grants, 15 second upload of 01's layout, 16 client switch off, 17 twenty sheets, 18 totals and factor columns) and `expected/*.json`; bytes are reproducible (fixed zip times and document properties). `workbooks/` and `results/` are git-ignored.
- `score.py` scores field mapping, site/category/period resolution, activity matches, out-of-scope rows flagged and never written, model calls (0 on a profile hit), hostile cell inert, the sheet's totals ignored, the client switch refused; thresholds in `THRESHOLDS` and `summarise()`.
- `runner.py` (standard library only) drives upload → propose → preview → paged rows → cross-tenant twin → import on the audit stack with fixture tokens; `--setup` sets the client switches; results one JSON per case.
- CI: `ci/tests/test_evals_set.py` (generator deterministic and in sync, expected files inside the fixture, scorer passes a perfect run and fails a flawed one, perfect proposals validate against `app.schemas.mapping` once XI0 2/2 is on the head).
- Audit harness: `/mnt/project-files/audit/harness/xi-probes.cjs`, step `xi-probes` in `run-audit.sh`, rows XI-01..08 in `audit/e2e-workflow-audit.md` §13. Rows that need XI2/XI3/XI5 report `na` until those merge.
- Not done here: the live eval run (needs XI2 on the head and the model key as a cloud-environment secret); results go to `audit/results/` and rows XI-02 / E2E-11.
- Review fixes (2026-10-14, review of the open XI PRs): the scorer scores every expected case (missing = failed), every computed check gates the pass, out-of-scope flags and writes are checked per row (`plan.out_of_scope_refs`, written rows read back as Superadmin), a missing sheet or unreported `model.calls` gets no credit; the runner is localhost-only, adds `depends_on` cases to `--only`, runs case 12 by default and refuses a stack holding earlier eval batches. Case 18's 8,877 tCO2e is the engine's value × factor_value / 1000 with the fixture's factor_value 2690 (not a unit error).
