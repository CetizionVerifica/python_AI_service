"""XI6 eval runner: drives every eval workbook through the smart import.

    uv run python ci/evals/runner.py [--only 01,15] [--full] [--setup] [--dry-run]
        [--api http://localhost:3996] [--ai http://localhost:8997] [--secret audit-local]
        [--out ci/evals/results/<timestamp>]

Runs against the THROWAWAY audit stack only (``/mnt/project-files/audit/
harness/start-stack.sh``: ESG-lite on :3996, this service on :8997, the
fixture of ``mcp-fixture.sql``, JWT secret ``audit-local``). Tokens are
signed here like the audit probes do (HS256 ``{userId, role, siteId}``), so
every call carries a real caller token and ESG-lite's own scoping applies:
the runner has no service credential. The model key the mapping engine
needs (``ANTHROPIC_API_KEY`` or ``OPENROUTER_API_KEY``) is the stack's, set
as a cloud-environment secret, never written here.

Flow per case (the XI4 screens, without the browser):
  upload → POST /v1/excel/propose → POST /v1/excel/preview (proposal) →
  page the plan's rows → [cross-tenant twin as company 2's Manager] →
  POST /v1/excel/import when the case commits.
Results go to one JSON per case; ``score.py`` reads them. Case 15 needs
case 01 to have committed first (profile learnt), so cases run in order.
Case 12 (15,000 rows) only runs with ``--full``.
Only standard-library modules, so it runs under plain ``python3`` too.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
WORKBOOKS = HERE / "workbooks"
EXPECTED = HERE / "expected"
OUT_OF_SCOPE_CODES = ("SITE_NOT_ALLOWED", "CATEGORY_NOT_ASSIGNED", "NOT_IN_CONTEXT")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
LARGE_CASES = ("12-",)


def require_loopback(url: str, flag: str) -> None:
    """The runner signs Superadmin tokens, flips client switches and commits
    imports, so it only ever talks to a stack on this machine (start-stack.sh)."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise SystemExit(f"refusing {flag} {url}: the eval runner only runs against the local throwaway stack (localhost)")


def with_dependencies(selected: list[dict], all_cases: list[dict]) -> list[dict]:
    """Add every case a selected case depends on (e.g. 15 needs 01 committed first), in set order."""
    by_name = {c["case"]: c for c in all_cases}
    wanted = {c["case"] for c in selected}
    stack = list(wanted)
    while stack:
        dep = by_name[stack.pop()].get("depends_on")
        if dep and dep not in wanted:
            wanted.add(dep)
            stack.append(dep)
    return [c for c in all_cases if c["case"] in wanted]

# Fixture users (ESG-lite ci/api/fixture.sql + mcp-fixture.sql).
CALLERS = {
    "user": (1, "User", 1),
    "manager": (2, "Manager", None),
    "admin": (3, "Admin", 1),
    "other_user": (4, "User", 3),
    "superadmin": (5, "Superadmin", None),
    "other_manager": (6, "Manager", None),
    "multi_user": (7, "User", None),
}


def token(secret: str, user_id: int, role: str, site_id: int | None) -> str:
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")  # noqa: E731
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"userId": user_id, "role": role, "siteId": site_id, "exp": int(time.time()) + 3600}).encode())
    sig = b64(hmac.new(secret.encode(), head + b"." + body, hashlib.sha256).digest())
    return (head + b"." + body + b"." + sig).decode()


def http(method: str, url: str, tok: str | None, data: dict | None = None, multipart: tuple | None = None, timeout: int = 300) -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    payload = None
    if multipart:
        fields, (fname, fbytes, fmime) = multipart
        bnd = "xi6evalboundary4c1a"
        payload = b""
        for k, v in fields.items():
            payload += f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        payload += f'--{bnd}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\nContent-Type: {fmime}\r\n\r\n'.encode() + fbytes + f"\r\n--{bnd}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={bnd}"
    elif data is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(data).encode()
    req = urllib.request.Request(url, method=method, headers=headers, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw[:300].decode("utf8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:300].decode("utf8", "replace")
    except Exception as e:  # connection refused, timeout
        return 0, str(e)


def error_code(body: object) -> str | None:
    """The IM0/XI0 error code of an error body, wherever ESG-lite or this service put it."""
    if not isinstance(body, dict):
        return None
    for key in ("code", "error"):
        if isinstance(body.get(key), str):
            return body[key]
    detail = body.get("detail")
    if isinstance(detail, dict):
        return error_code(detail)
    if isinstance(detail, str) and detail.isupper():
        return detail
    return None


def plan_summary(body: object) -> dict:
    plan = (body or {}).get("plan") if isinstance(body, dict) else None
    return plan if isinstance(plan, dict) else {}


class Stack:
    def __init__(self, api: str, ai: str, secret: str) -> None:
        self.api, self.ai, self.secret = api.rstrip("/"), ai.rstrip("/"), secret

    def tok(self, caller: str) -> str:
        return token(self.secret, *CALLERS[caller])

    def setup(self) -> None:
        """Client switch on for company 1 only, as Superadmin does on P17."""
        sa = self.tok("superadmin")
        for company_id, on in ((1, True), (2, False)):
            code, body = http("PUT", f"{self.api}/admin/companies/{company_id}", sa, {"ai_import_enabled": on})
            print(f"setup: company {company_id} ai_import_enabled={on} -> HTTP {code}")

    def upload(self, caller: str, path: pathlib.Path) -> tuple[int, int | None, object]:
        code, body = http("POST", f"{self.ai}/v1/excel/upload", self.tok(caller), multipart=({}, (path.name, path.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")))
        return code, (body.get("document_id") if isinstance(body, dict) else None), body

    def earlier_eval_batches(self, names: set[str]) -> list[str]:
        """Upload batches an earlier eval run committed on this stack. A profile
        learnt then would turn later proposals into profile hits, so a run must
        start from a fresh stack (start-stack.sh reloads the fixture)."""
        code, body = http("GET", f"{self.api}/imports/batches?company_id=1", self.tok("superadmin"))
        if code != 200 or not isinstance(body, list):
            return []
        return sorted({str(b.get("source_name")) for b in body if isinstance(b, dict) and str(b.get("source_name") or "") in names})

    def written_rows(self, batch_id: str) -> list[dict]:
        """Every row the batch wrote, read as Superadmin so rows outside the caller's scope would show too."""
        out: list[dict] = []
        page = 1
        while True:
            q = urllib.parse.urlencode({"batchId": batch_id, "page": page, "limit": 500})
            code, body = http("GET", f"{self.api}/user/emissions?{q}", self.tok("superadmin"))
            if code != 200:
                break
            items = body if isinstance(body, list) else next((v for v in (body or {}).values() if isinstance(v, list)), []) if isinstance(body, dict) else []
            for r in items:
                if not isinstance(r, dict):
                    continue
                out.append({
                    "site_id": r.get("site_id") if r.get("site_id") is not None else (r.get("site") or {}).get("site_id"),
                    "category_id": r.get("category_id") if r.get("category_id") is not None else (r.get("category") or {}).get("category_id"),
                    "row_ref": r.get("import_row_ref") or r.get("row_ref"),
                })
            if len(items) < 500:
                break
            page += 1
        return out

    def page_rows(self, caller: str, document_id: int, site_id: int, proposal: dict, plan_id: str, company_id: int | None) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while True:
            body = {"document_id": document_id, "site_id": site_id, "proposal": proposal, "plan_id": plan_id, "page": page, "page_size": 500}
            if company_id:
                body["company_id"] = company_id
            code, res = http("POST", f"{self.ai}/v1/excel/preview", self.tok(caller), body)
            if code != 200 or not isinstance(res, dict):
                break
            got = res.get("rows") or []
            rows.extend(got)
            total = int(res.get("total_rows") or 0)
            if not got or len(rows) >= total:
                break
            page += 1
        return rows


def run_case(stack: Stack, exp: dict, hostile_text: str | None) -> dict:
    name = exp["case"]
    caller = exp["caller"]
    site_id = exp["site_id"]
    company_id = {1: 1, 2: 1, 3: 2}.get(site_id) if caller == "superadmin" else None
    result: dict = {"case": name, "caller": caller, "site_id": site_id, "status": {}, "error_code": None, "proposal": None, "plan": None, "import": None, "model_calls": None, "profile_hits": None, "hostile_in_data": None, "cross_tenant": None, "error": None, "ran_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    path = WORKBOOKS / exp["workbook"]
    if not path.exists():
        result["error"] = f"missing workbook {path.name}; run make_workbooks.py"
        return result

    code, document_id, body = stack.upload(caller, path)
    result["status"]["upload"] = code
    if code != 200 or not document_id:
        result["error"] = f"upload failed: HTTP {code} {str(body)[:160]}"
        return result

    propose_body: dict = {"document_id": document_id, "site_id": site_id}
    if company_id:
        propose_body["company_id"] = company_id
    code, body = http("POST", f"{stack.ai}/v1/excel/propose", stack.tok(caller), propose_body)
    result["status"]["propose"] = code
    result["error_code"] = error_code(body)
    if code == 404 and (not isinstance(body, dict) or error_code(body) in (None, "Not Found")):
        result["error"] = "POST /v1/excel/propose is not built on this head (XI2)"
        return result
    if code != 200:
        result["propose_body"] = body if isinstance(body, dict) else str(body)[:300]
        result["model_calls"] = 0
        return result
    proposal = body if isinstance(body, dict) else {}
    result["proposal"] = proposal
    model = proposal.get("model") or {}
    result["model_calls"] = model.get("calls")
    result["profile_hits"] = model.get("cached_profile_hits")

    preview_body: dict = {"document_id": document_id, "site_id": site_id, "proposal": proposal}
    if company_id:
        preview_body["company_id"] = company_id
    code, body = http("POST", f"{stack.ai}/v1/excel/preview", stack.tok(caller), preview_body)
    result["status"]["preview"] = code
    summary = plan_summary(body)
    if code != 200 or not summary:
        result["error"] = f"preview failed: HTTP {code} {str(body)[:200]}"
        return result
    issues = {i.get("code"): i.get("count") for i in summary.get("issues_summary") or [] if isinstance(i, dict)}
    rows = stack.page_rows(caller, document_id, site_id, proposal, summary.get("plan_id"), company_id) if summary.get("plan_id") else list((body or {}).get("rows") or [])
    flagged_refs = sorted({str(r.get("row_ref")) for r in rows if any(i.get("code") in OUT_OF_SCOPE_CODES for i in r.get("issues") or [])})
    result["plan"] = {"plan_id": summary.get("plan_id"), "version": summary.get("version"), "counts": summary.get("counts"), "totals": summary.get("totals"), "issues": issues, "out_of_scope_rows": len(flagged_refs), "flagged_refs": flagged_refs, "issue_refs": sorted({str(r.get("row_ref")) for r in rows if r.get("state") == "issue"}), "paged_rows": len(rows)}
    if hostile_text:
        h = hostile_text.lower()
        result["hostile_in_data"] = any(str(r.get("emission_category") or "").lower() == h or str(r.get("notes") or r.get("Notes") or "").lower() == h for r in rows)

    if exp.get("cross_tenant_probe"):
        code, body = http("POST", f"{stack.ai}/v1/excel/preview", stack.tok("other_manager"), preview_body)
        result["cross_tenant"] = {"caller": "other_manager", "status": code, "body": str(body)[:160]}

    if exp.get("commit") and summary.get("plan_id"):
        import_body = {**preview_body, "plan_id": summary["plan_id"], "version": summary.get("version")}
        code, body = http("POST", f"{stack.ai}/v1/excel/import", stack.tok(caller), import_body)
        result["status"]["import"] = code
        if code == 200 and isinstance(body, dict):
            result["import"] = {"inserted": body.get("inserted"), "skipped": body.get("skipped"), "upload_batch_id": body.get("upload_batch_id")}
            if body.get("upload_batch_id"):
                result["written"] = stack.written_rows(str(body["upload_batch_id"]))
        else:
            result["import_error"] = str(body)[:200]
    elif summary.get("plan_id"):
        http("POST", f"{stack.api}/imports/plans/{summary['plan_id']}/cancel", stack.tok(caller), {} if not company_id else {"company_id": company_id})
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:3996")
    ap.add_argument("--ai", default="http://localhost:8997")
    ap.add_argument("--secret", default="audit-local")
    ap.add_argument("--only", default="", help="comma-separated case prefixes, e.g. 01,15 (cases they depend on are added)")
    ap.add_argument("--skip-large", action="store_true", help="leave out the 15,000-row case 12 (the score then reports it missing)")
    ap.add_argument("--allow-warm-stack", action="store_true", help="run even though an earlier eval run committed on this stack (results not comparable)")
    ap.add_argument("--setup", action="store_true", help="turn ai_import_enabled on for company 1 and off for company 2 first")
    ap.add_argument("--dry-run", action="store_true", help="list the cases and stop")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv[1:])

    require_loopback(args.api, "--api")
    require_loopback(args.ai, "--ai")
    all_cases = [json.loads(f.read_text()) for f in sorted(EXPECTED.glob("*.json"))]
    cases = all_cases
    only = [p.strip() for p in args.only.split(",") if p.strip()]
    if only:
        cases = with_dependencies([c for c in all_cases if any(c["case"].startswith(p) for p in only)], all_cases)
    if args.skip_large:
        cases = [c for c in cases if not c["case"].startswith(LARGE_CASES)]
    if args.dry_run:
        for c in cases:
            print(f"{c['case']}: {c['caller']} site {c['site_id']} → HTTP {c['expect_status']}, {len(c['sheets'])} sheet(s), {c['plan']['rows']} rows, commit={c['commit']}")
        return 0

    out = pathlib.Path(args.out) if args.out else HERE / "results" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    stack = Stack(args.api, args.ai, args.secret)
    if args.setup:
        stack.setup()
    earlier = stack.earlier_eval_batches({c["workbook"] for c in all_cases})
    if earlier and not args.allow_warm_stack:
        print(f"refusing: this stack already has eval batches ({', '.join(earlier[:3])}…); restart start-stack.sh for a fresh, comparable run", file=sys.stderr)
        return 3
    for exp in cases:
        t0 = time.time()
        result = run_case(stack, exp, exp.get("hostile_text"))
        result["seconds"] = round(time.time() - t0, 1)
        (out / f"{exp['case']}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        st = result["status"]
        line = result["error"] or f"propose {st.get('propose')} preview {st.get('preview')} import {st.get('import', '-')} calls={result.get('model_calls')} rows={(result.get('plan') or {}).get('counts', {}).get('rows') if result.get('plan') else '-'} oos={(result.get('plan') or {}).get('out_of_scope_rows') if result.get('plan') else '-'}"
        print(f"{exp['case']}: {line} ({result['seconds']}s)")
    print(f"results in {out}; score with: uv run python ci/evals/score.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
