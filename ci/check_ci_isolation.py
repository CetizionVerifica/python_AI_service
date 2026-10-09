#!/usr/bin/env python3
"""Keep CI away from production.

CI runs on every pull request, including ones from branches nobody has
reviewed yet, so it must never hold a credential that reaches real data.
This fails when a workflow file references any repository secret (other than
the automatic, repo-scoped GITHUB_TOKEN) or names a production database
variable.
"""
import pathlib
import re
import sys

ALLOWED_SECRETS = {"GITHUB_TOKEN"}
SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
PROD_RE = re.compile(r"\b(DATABASE_URL|PROD(UCTION)?_[A-Z_]*(URL|HOST|PASSWORD|KEY))\b")

problems = []
for wf in sorted(pathlib.Path(".github/workflows").glob("*.y*ml")):
    for no, line in enumerate(wf.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for name in SECRET_RE.findall(line):
            if name not in ALLOWED_SECRETS:
                problems.append(f"{wf}:{no}: uses secrets.{name}")
        if PROD_RE.search(line):
            problems.append(f"{wf}:{no}: names a production connection variable")

if problems:
    print("CI must not have access to production. Remove these:")
    for p in problems:
        print("  " + p)
        print(f"::error::{p}")
    sys.exit(1)
print("Workflows use no secrets and no production connection settings.")
