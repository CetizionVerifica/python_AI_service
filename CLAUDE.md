# python_AI_service

## Redesign and PCF work

- All redesign and PCF PRs target the long-lived staging branch `production` (formerly `redesign/integration`), not `main`. Branch from it and open PRs against it.
- Spec for this repo: `docs/pcf/foundation/E2-ai-assist/CLAUDE.md` (PCF AI assist endpoints).
- The full plans live in ESG-lite_FE under `docs/redesign/` and `docs/pcf/`.

## Imports — see ESG-lite docs/mcp-import/
- Bulk import rows go to ESG-lite `POST /imports/plans` with the caller's token. This service reads and maps sheets; it doesn't calculate or insert emission rows.

## Scope 3 — see ESG-lite docs/scope3/
- `app/scope3/` and the factor sheet parser read and map data only: they keep WTT and per-gas columns as separate factor types and may suggest an EEIO sector or a GHG category with a confidence. They never calculate totals or choose the method for a row.
