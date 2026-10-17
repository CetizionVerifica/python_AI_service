# XI0 shared fixtures

Identical copies live in python_AI_service `ci/fixtures/xi0/`. Both repos' contract tests parse every file here; change a file in both repos in the same wave.

- `proposal-valid.json` parses. `proposal-other-site.json` (a sheet for another site), `proposal-bad-fingerprint.json` and `proposal-confidence-over-1.json` must be refused.
- `row-with-meta.json` is an ImportRow carrying `meta`; `profile-valid.json` a MappingProfile.
