"""XI6 eval scorer: compares a runner results folder with ``expected/*.json``.

    uv run python ci/evals/score.py ci/evals/results/<timestamp>

One line per case, then ``SUMMARY {...}`` and ``summary.json`` in the folder.
Exit 0 when the acceptance holds (00-plan.md §12 XM-3): field mapping
accuracy ≥ 95 %, every out-of-scope row flagged, 0 cross-tenant writes,
hostile cells inert, zero model calls on a profile hit, the client switch
refused, the sheet's own totals ignored.

A result file (one per case, written by ``runner.py``) carries:
  case, status {upload, propose, preview, import}, error_code, proposal
  (the MappingProposal as returned, or null), plan {counts, issues, totals,
  out_of_scope_rows}, import {inserted, skipped} or null, model_calls,
  profile_hits, hostile_in_data, cross_tenant {status} or null, error.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
EXPECTED_DIR = HERE / "expected"
OUT_OF_SCOPE_CODES = ("SITE_NOT_ALLOWED", "CATEGORY_NOT_ASSIGNED", "NOT_IN_CONTEXT")
THRESHOLDS = {"fields": 0.95, "resolution": 0.90, "activities": 0.90}


def norm(value) -> str:
    """Case, whitespace and punctuation insensitive text compare."""
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def norm_key(key) -> str:
    k = norm(key)
    return k[6:] if k.startswith("extra ") else k


def find_sheet(proposal: dict | None, sheet_name: str) -> dict | None:
    for s in (proposal or {}).get("sheets") or []:
        if norm(s.get("sheet_name")) == norm(sheet_name):
            return s
    return None


class Tally:
    def __init__(self) -> None:
        self.correct = 0
        self.total = 0
        self.misses: list[str] = []

    def add(self, ok: bool, what: str) -> None:
        self.total += 1
        if ok:
            self.correct += 1
        else:
            self.misses.append(what)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 1.0


def score_fields(exp_sheet: dict, got: dict | None, tally: Tally) -> None:
    fields = {norm_key(f.get("key")): f for f in (got or {}).get("fields") or []}
    for key, column in exp_sheet["fields"].items():
        f = fields.get(norm_key(key))
        if column is None:
            ok = f is None or f.get("column") is None or f.get("skipped") is True
        else:
            ok = f is not None and norm(f.get("column")) == norm(column) and not f.get("skipped")
        tally.add(ok, f"{exp_sheet['sheet_name']}.{key}: want {column!r}, got {(f or {}).get('column')!r}")
    mapped = {norm(f.get("column")) for f in fields.values() if f.get("column")}
    for column in exp_sheet.get("never_map_columns") or []:
        tally.add(norm(column) not in mapped, f"{exp_sheet['sheet_name']}: column {column!r} must never be mapped")


def score_resolution(exp_sheet: dict, got: dict | None, tally: Tally) -> None:
    name = exp_sheet["sheet_name"]
    cat = (got or {}).get("category") or {}
    if exp_sheet.get("category_column"):
        ok = cat.get("kind") == "column" and norm(cat.get("column")) == norm(exp_sheet["category_column"]) and all(v.get("id") for v in cat.get("values") or [])
    elif exp_sheet.get("category_id") is None:
        # Not in the caller's context: no category id anywhere on the sheet.
        ids = [cat.get("id")] if cat.get("kind") == "fixed" else [v.get("id") for v in cat.get("values") or []]
        ok = got is not None and not any(ids) or bool((got or {}).get("not_in_context")) or bool((got or {}).get("excluded"))
    else:
        ok = cat.get("kind") == "fixed" and cat.get("id") == exp_sheet["category_id"]
    tally.add(ok, f"{name}.category: want {exp_sheet.get('category_column') or exp_sheet.get('category_id')!r}, got {json.dumps(cat)[:80]}")

    want_p, got_p = exp_sheet["period"], (got or {}).get("period") or {}
    if want_p["kind"] == "column":
        ok = got_p.get("kind") == "column" and norm(got_p.get("column")) == norm(want_p["column"])
    elif want_p["kind"] == "yearly":
        ok = got_p.get("kind") == "yearly" and got_p.get("year_type") == want_p["year_type"] and got_p.get("year") == want_p["year"]
    else:
        ok = got_p.get("kind") == want_p["kind"]
    tally.add(ok, f"{name}.period: want {want_p}, got {got_p}")

    if exp_sheet.get("site_column"):
        sc = (got or {}).get("site_column") or {}
        values = {norm(v.get("text")): v.get("matches_site") for v in sc.get("values") or []}
        ok = norm(sc.get("column")) == norm(exp_sheet["site_column"])
        tally.add(ok, f"{name}.site_column: want {exp_sheet['site_column']!r}, got {sc.get('column')!r}")
        for text in exp_sheet.get("out_of_scope_values") or []:
            tally.add(values.get(norm(text)) is False, f"{name}.site_column: {text!r} must be matches_site=false, got {values.get(norm(text))!r}")

    if "data_rows" in exp_sheet and got is not None:
        tally.add(got.get("data_rows") == exp_sheet["data_rows"], f"{name}.data_rows: want {exp_sheet['data_rows']}, got {got.get('data_rows')}")


def score_activities(exp_sheet: dict, got: dict | None, tally: Tally) -> None:
    acts = {norm(a.get("sheet_text")): a for a in (got or {}).get("activities") or []}
    for text, factor in exp_sheet["activities"].items():
        a = acts.get(norm(text))
        if factor is None:
            ok = a is not None and a.get("factor_name") is None
        else:
            ok = a is not None and norm(a.get("factor_name")) == norm(factor)
        tally.add(ok, f"{exp_sheet['sheet_name']}.activity {text[:30]!r}: want {factor!r}, got {(a or {}).get('factor_name')!r}")


def proposal_without_sheet_text(proposal: dict | None) -> str:
    """The proposal as text, minus the places where sheet wording is allowed to live."""
    if not proposal:
        return ""
    copy = json.loads(json.dumps(proposal))
    for s in copy.get("sheets") or []:
        for a in s.get("activities") or []:
            a.pop("sheet_text", None)
        for v in (s.get("category") or {}).get("values") or []:
            v.pop("text", None)
        for v in (s.get("site_column") or {}).get("values") or []:
            v.pop("text", None)
        s.pop("not_in_context", None)
    return json.dumps(copy, ensure_ascii=False).lower()


def score_case(result: dict, exp: dict) -> dict:
    name = exp["case"]
    out: dict = {"case": name, "caller": exp["caller"]}
    if result.get("error"):
        return {**out, "error": result["error"]}
    status = result.get("status") or {}
    out["status_ok"] = status.get("propose") == exp["expect_status"] and (not exp.get("expect_error") or result.get("error_code") == exp["expect_error"])
    out["propose_status"] = status.get("propose")
    if exp["expect_status"] != 200:
        out["switch_off_ok"] = out["status_ok"] and not result.get("proposal") and (result.get("model_calls") or 0) == 0
        return out

    proposal = result.get("proposal")
    fields, resolution, activities = Tally(), Tally(), Tally()
    for exp_sheet in exp["sheets"]:
        got = find_sheet(proposal, exp_sheet["sheet_name"])
        score_fields(exp_sheet, got, fields)
        score_resolution(exp_sheet, got, resolution)
        score_activities(exp_sheet, got, activities)
    out["sheets"] = f"{len((proposal or {}).get('sheets') or [])}/{len(exp['sheets'])}"
    out["fields"] = f"{fields.correct}/{fields.total}"
    out["resolution"] = f"{resolution.correct}/{resolution.total}"
    out["activities"] = f"{activities.correct}/{activities.total}"
    out["_tallies"] = {"fields": (fields.correct, fields.total), "resolution": (resolution.correct, resolution.total), "activities": (activities.correct, activities.total)}
    out["misses"] = (fields.misses + resolution.misses + activities.misses)[:12]

    plan = result.get("plan") or {}
    counts = plan.get("counts") or {}
    want_plan = exp["plan"]
    out["rows"] = f"{counts.get('rows')}/{want_plan['rows']}"
    out["rows_ok"] = counts.get("rows") == want_plan["rows"]
    flagged = int(plan.get("out_of_scope_rows") or 0)
    want_oos = int(want_plan.get("out_of_scope_rows") or 0)
    out["out_of_scope"] = f"{flagged}/{want_oos}"
    out["out_of_scope_flagged"] = 1.0 if want_oos == 0 else min(flagged, want_oos) / want_oos
    imported = (result.get("import") or {}).get("inserted")
    out["out_of_scope_written"] = 0 if imported is None else max(0, int(imported) - (want_plan["rows"] - want_oos))
    issues = plan.get("issues") or {}
    issue_checks = []
    for code, want in want_plan.get("issues", {}).items():
        have = issues.get(code)
        issue_checks.append((have is not None and have > 0) if want == "warning" else have == want)
    out["issues_ok"] = all(issue_checks)
    out["issues"] = issues

    out["model_calls"] = result.get("model_calls")
    out["model_calls_ok"] = (result.get("model_calls") or 0) <= exp["max_model_calls"]
    out["profile_ok"] = (result.get("profile_hits") or 0) >= exp["min_profile_hits"]
    if "source_profile" in exp.get("checks", []):
        out["profile_ok"] = out["profile_ok"] and all(s.get("source") == "profile" for s in (proposal or {}).get("sheets") or [])
    if "activity_needs_confirmation" in exp.get("checks", []):
        acts = [a for s in (proposal or {}).get("sheets") or [] for a in s.get("activities") or []]
        out["needs_confirmation_ok"] = bool(acts) and all(a.get("confidence", 1) < 0.8 or a.get("source") == "llm" for a in acts)
    if "header_row_3" in exp.get("checks", []):
        out["header_row_ok"] = all(s.get("header_row") == 3 for s in (proposal or {}).get("sheets") or [])

    if exp.get("hostile_text"):
        h = exp["hostile_text"].lower()
        out["hostile_ok"] = h not in proposal_without_sheet_text(proposal) and bool(result.get("hostile_in_data"))
    if want_plan.get("tco2e") is not None:
        got_t = (plan.get("totals") or {}).get("tco2e")
        out["tco2e"] = f"{got_t}/{want_plan['tco2e']}"
        out["totals_ok"] = got_t is not None and abs(float(got_t) - float(want_plan["tco2e"])) < 0.01
    if exp.get("cross_tenant_probe"):
        ct = (result.get("cross_tenant") or {}).get("status")
        out["cross_tenant_ok"] = ct in (401, 403, 404)
        out["cross_tenant_status"] = ct
    if exp.get("commit"):
        out["committed"] = imported is not None
    return out


def summarise(scores: list[dict]) -> dict:
    ok = [s for s in scores if not s.get("error")]
    tallies = {k: [0, 0] for k in ("fields", "resolution", "activities")}
    for s in ok:
        for k, (c, t) in (s.get("_tallies") or {}).items():
            tallies[k][0] += c
            tallies[k][1] += t
    acc = {k: (round(c / t, 4) if t else 1.0) for k, (c, t) in tallies.items()}
    all_true = lambda key: all(s.get(key, True) for s in ok)  # noqa: E731
    summary = {
        "cases": len(scores),
        "failed_runs": len(scores) - len(ok),
        "status_ok": all_true("status_ok"),
        "field_accuracy": acc["fields"],
        "resolution_accuracy": acc["resolution"],
        "activity_accuracy": acc["activities"],
        "rows_ok": all_true("rows_ok"),
        "issues_ok": all_true("issues_ok"),
        "out_of_scope_flagged": round(min([s.get("out_of_scope_flagged", 1.0) for s in ok] or [1.0]), 4),
        "cross_tenant_writes": sum(s.get("out_of_scope_written", 0) for s in ok) + sum(1 for s in ok if s.get("cross_tenant_ok") is False),
        "model_calls_ok": all_true("model_calls_ok"),
        "profile_ok": all_true("profile_ok"),
        "hostile_ok": all_true("hostile_ok"),
        "totals_ok": all_true("totals_ok"),
        "switch_off_ok": all_true("switch_off_ok"),
        "needs_confirmation_ok": all_true("needs_confirmation_ok"),
    }
    summary["pass"] = (
        summary["failed_runs"] == 0
        and summary["status_ok"]
        and summary["field_accuracy"] >= THRESHOLDS["fields"]
        and summary["resolution_accuracy"] >= THRESHOLDS["resolution"]
        and summary["activity_accuracy"] >= THRESHOLDS["activities"]
        and summary["out_of_scope_flagged"] == 1.0
        and summary["cross_tenant_writes"] == 0
        and summary["model_calls_ok"]
        and summary["profile_ok"]
        and summary["hostile_ok"]
        and summary["totals_ok"]
        and summary["switch_off_ok"]
    )
    return summary


def score_dir(results_dir: pathlib.Path, expected_dir: pathlib.Path = EXPECTED_DIR) -> tuple[list[dict], dict]:
    scores = []
    for f in sorted(results_dir.glob("*.json")):
        if f.name == "summary.json":
            continue
        result = json.loads(f.read_text())
        name = result.get("case") or f.stem
        exp_file = expected_dir / f"{name}.json"
        if not exp_file.exists():
            scores.append({"case": name, "error": "no expected file"})
            continue
        scores.append(score_case(result, json.loads(exp_file.read_text())))
    return scores, summarise(scores)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: score.py <results dir>", file=sys.stderr)
        return 2
    results_dir = pathlib.Path(argv[1])
    scores, summary = score_dir(results_dir)
    for s in scores:
        print(json.dumps({k: v for k, v in s.items() if not k.startswith("_")}, ensure_ascii=False))
    print("SUMMARY " + json.dumps(summary))
    (results_dir / "summary.json").write_text(json.dumps({"scores": [{k: v for k, v in s.items() if not k.startswith("_")} for s in scores], "summary": summary}, indent=2, ensure_ascii=False) + "\n")
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
