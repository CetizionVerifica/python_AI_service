# python_AI_service

## Redesign and PCF work

- All redesign and PCF PRs target the long-lived integration branch `redesign/integration`, not `main`. Branch from it and open PRs against it.
- Spec for this repo: `docs/pcf/foundation/E2-ai-assist/CLAUDE.md` (PCF AI assist endpoints).
- The full plans live in ESG-lite_FE under `docs/redesign/` and `docs/pcf/`.

## Imports — see ESG-lite docs/mcp-import/
- Bulk import rows go to ESG-lite `POST /imports/plans` with the caller's token. This service reads and maps sheets; it doesn't calculate or insert emission rows.
