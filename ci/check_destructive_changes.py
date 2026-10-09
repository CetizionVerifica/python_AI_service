#!/usr/bin/env python3
"""Fail the build when a change adds code that can destroy production data.

Scans only the lines this branch ADDS (git diff against the base branch), so
existing code never trips it. Two things are caught:

  * schema changes that lose data: DROP TABLE/COLUMN/SCHEMA/DATABASE,
    TRUNCATE, RENAME of a table or column, column type changes
    (ALTER COLUMN ... TYPE), DELETE FROM without a WHERE, and the ORM
    equivalents (dropTable/dropColumn/renameColumn/.clear(), synchronize:true,
    dropSchema, TYPEORM_SYNC=true).
  * bulk data rewrites in one-off scripts (scripts/seeds/migrations folders):
    any DELETE FROM, or UPDATE ... SET without a WHERE.

A flagged line passes only when BOTH are true:
  1. the line, or the line right above it, carries a marker comment
         data-loss-reviewed: <why this is safe and how data is preserved>
  2. the pull request has the label `data-loss-reviewed` (set by a reviewer,
     passed in by the workflow as DATA_LOSS_REVIEWED=true).

Usage: python3 ci/check_destructive_changes.py <base-ref>
Only the standard library is used, so it runs on any CI runner.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

MARKER = re.compile(r"data-loss-reviewed:\s*\S", re.IGNORECASE)

CODE_EXTENSIONS = (
    ".ts", ".tsx", ".js", ".cjs", ".mjs", ".jsx", ".py", ".sql", ".sh",
    ".json", ".yml", ".yaml", ".env", "Dockerfile", ".toml",
)

# This checker's own files and fixtures describe the patterns; never scan them.
SKIP_PREFIXES = ("ci/",)

# Folders holding one-off scripts that run straight against a database.
SCRIPT_DIRS = re.compile(r"(^|/)(scripts|seeds|migrations|migration)/", re.IGNORECASE)

ALWAYS = [
    (re.compile(r"\bDROP\s+(TABLE|COLUMN|SCHEMA|DATABASE|VIEW|MATERIALIZED\s+VIEW)\b", re.I),
     "drops a table, column, schema or view"),
    (re.compile(r"\bALTER\s+TABLE\b[^;]*\bDROP\b(?!\s+(CONSTRAINT|INDEX|DEFAULT|NOT\s+NULL))", re.I),
     "drops a column"),
    (re.compile(r"\bTRUNCATE\b(\s+TABLE)?\s+[\"`\w]", re.I), "truncates a table"),
    (re.compile(r"\bRENAME\s+(COLUMN\s+|TO\s+)", re.I), "renames a table or column"),
    (re.compile(r"\bALTER\s+COLUMN\b[^;]*\b(TYPE|SET\s+DATA\s+TYPE)\b", re.I),
     "changes a column type"),
    (re.compile(r"\bDELETE\s+FROM\s+[\"`\w.]+\s*(;|[\"'`]|\)|$)(?![^;]*\bWHERE\b)", re.I),
     "deletes every row of a table"),
    (re.compile(r"\.(dropTable|dropColumn|dropColumns|renameColumn|renameTable|changeColumn|clearTable)\s*\("),
     "ORM call that drops, renames or retypes schema"),
    (re.compile(r"\.clear\s*\(\s*\)"), "ORM repository .clear() deletes every row"),
    (re.compile(r"\bsynchronize\s*:\s*true\b"),
     "TypeORM synchronize:true rewrites the live schema on boot"),
    (re.compile(r"\bdropSchema\s*:\s*true\b"), "TypeORM dropSchema wipes the database"),
    (re.compile(r"\bTYPEORM_SYNC\s*[=:]\s*[\"']?true\b", re.I),
     "TYPEORM_SYNC=true rewrites the live schema on boot"),
]

SCRIPTS_ONLY = [
    (re.compile(r"\bDELETE\s+FROM\b", re.I), "one-off script deletes rows"),
    (re.compile(r"\.delete\s*\(|\.remove\s*\(|createQueryBuilder\([^)]*\)\s*\.delete\(", re.I),
     "one-off script deletes rows through the ORM"),
    (re.compile(r"\bUPDATE\s+[\"`\w.]+\s+SET\b(?![^;]*\bWHERE\b)", re.I),
     "one-off script updates every row of a table"),
]


def added_lines(base: str):
    """Yield (path, line_no, text, previous_text) for every added line."""
    diff = subprocess.run(
        ["git", "diff", "--unified=1", "--no-color", "--diff-filter=AM", f"{base}...HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    path = None
    new_no = 0
    prev = ""
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_no = int(m.group(1)) if m else 0
            prev = ""
            continue
        if path is None or raw.startswith("--- "):
            continue
        if raw.startswith("+"):
            text = raw[1:]
            yield path, new_no, text, prev
            prev = text
            new_no += 1
        elif raw.startswith(" "):
            prev = raw[1:]
            new_no += 1
        # removed lines ("-") do not advance the new-file counter


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_destructive_changes.py <base-ref>", file=sys.stderr)
        return 2
    base = sys.argv[1]
    label_ok = os.environ.get("DATA_LOSS_REVIEWED", "").lower() == "true"

    findings = []
    for path, line_no, text, prev in added_lines(base):
        if path.startswith(SKIP_PREFIXES):
            continue
        if not (path.endswith(CODE_EXTENSIONS) or os.path.basename(path).startswith(".env")):
            continue
        rules = list(ALWAYS)
        if SCRIPT_DIRS.search(path):
            rules += SCRIPTS_ONLY
        for pattern, why in rules:
            if pattern.search(text):
                marked = bool(MARKER.search(text) or MARKER.search(prev))
                findings.append((path, line_no, text.strip(), why, marked))
                break

    if not findings:
        print("No destructive schema or data changes added by this branch.")
        return 0

    blocking = [f for f in findings if not (f[4] and label_ok)]
    for path, line_no, text, why, marked in findings:
        state = "approved" if (marked and label_ok) else ("marked, needs label" if marked else "BLOCKED")
        print(f"[{state}] {path}:{line_no}: {why}\n    {text[:200]}")
        if state != "approved":
            print(f"::error file={path},line={line_no}::{why}. Production data could be lost.")

    if blocking:
        print(
            "\nThis branch adds changes that can destroy production data.\n"
            "If the change is intended: write the data-preserving plan (backup, copy to the new\n"
            "column/table first, then remove the old one in a later release), add a comment\n"
            "`data-loss-reviewed: <reason>` on or above each flagged line, and ask a reviewer to\n"
            "add the `data-loss-reviewed` label to the pull request."
        )
        return 1
    print("\nAll flagged lines are marked and the PR carries the data-loss-reviewed label.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
