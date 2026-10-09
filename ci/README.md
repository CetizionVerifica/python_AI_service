# CI safety checks

`.github/workflows/ci.yml` runs on every pull request into `redesign/integration`
or `main`, and on pushes to them. It keeps production data safe while the
redesign is built. CI never connects to production: the workflow holds no
secrets, and the database tests refuse anything but the throwaway Postgres
container (`ci/tests/conftest.py`).

| Check | What fails it |
|---|---|
| CI has no production access | a workflow references a secret or a production DB variable |
| No destructive schema or data changes | an added line drops, truncates, renames or retypes schema, or deletes every row; or a file in `scripts/` deletes or bulk-updates rows |
| Install with `uv sync --locked` | `uv.lock` out of date with `pyproject.toml` |
| Compile and lint (`ruff` E9/F63/F7/F82) | syntax errors, undefined names |
| `ci/tests/test_calculation_snapshot.py` | spec products, the activity-value heuristic, unit conversions, unit/number/date parsing, rounding, invoice total checks or category mapping change |
| `ci/tests/test_import_snapshot.py` | a spreadsheet bulk import writes different rows or totals, or this service's SQL no longer fits the ESG-lite schema |
| `ci/tests/test_startup_tables.py` | the startup `CREATE TABLE IF NOT EXISTS` steps fail on a second boot or lose rows |

## When a check blocks you on purpose

**Destructive change you really need.** Ship it as expand-then-contract (add
and backfill first, remove the old thing in a later release, after a backup),
put `# data-loss-reviewed: <why this is safe>` on or above each flagged line,
and ask a reviewer to add the `data-loss-reviewed` label to the PR. Both are
required.

**Deliberate calculation change.** Rerun against a local throwaway database
and commit the new snapshots, listing every moved figure in the PR:

```bash
createdb ci_ai
export CI_THROWAWAY_DB=true DB_HOST=localhost DB_PORT=5432 DB_USERNAME=postgres DB_PASSWORD=postgres DB_NAME=ci_ai TZ=UTC PYTHONPATH=.
UPDATE_SNAPSHOT=1 uv run --with pytest pytest ci/tests
```

**ESG-lite schema changed.** `ci/fixtures/esg_lite_schema.sql` is a copy of the
schema the Node backend's entities produce. After an ESG-lite migration lands,
regenerate it with `pg_dump --schema-only --no-owner --no-privileges` from a
database built by ESG-lite's `ci/schema-sync.cjs`, then drop the `\restrict`
lines.
