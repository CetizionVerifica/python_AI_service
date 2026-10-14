"""IM11: the bulk upload runs on ESG-lite's plans API.

Mapped sheet rows become ImportRows (01-contracts.md §1), go to
POST /imports/plans in chunks of 2,000 with the caller's own token and
channel "bulk_upload", and are committed by ESG-lite. This service never
inserts emission rows. ESG-lite is a recorded fake here (httpx MockTransport);
the API tests below run the real routes against the throwaway database for
the document, the sign-in and the one-import-per-file claim.
"""
import json
import os
import time

import httpx
import jwt
import pandas as pd
import pytest

from app.services import excel_plan_client as plans

SITE, CATEGORY = 1, 1
MONTH_END = "2025-06-30"


# ---------------------------------------------------------------------------
# A fake ESG-lite
# ---------------------------------------------------------------------------


class FakeEsgLite:
    """Answers the plans API the way ESG-lite does, recording every request."""

    def __init__(self, fail=None):
        self.requests: list[dict] = []
        self.plans: dict[str, dict] = {}
        self.fail = fail  # (path suffix, status, body) to answer with an error
        self.counter = 0

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _plan(self, plan_id):
        p = self.plans[plan_id]
        ready = [r for r in p["rows"] if r["state"] == "ready"]
        issue = [r for r in p["rows"] if r["state"] == "issue"]
        return {
            "plan_id": plan_id,
            "version": p["version"],
            "status": p["status"],
            "counts": {"rows": len(p["rows"]), "ready": len(ready), "issue": len(issue), "skipped": 0, "warnings": 0},
            "totals": {"tco2e": round(sum(r["result"]["total_tco2e"] for r in ready), 6), "by_category": [], "by_month": []},
            "issues_summary": [{"code": "UNKNOWN_ACTIVITY", "count": len(issue), "example_row_refs": [r["row_ref"] for r in issue][:5]}] if issue else [],
            "expires_at": "2026-01-01T00:00:00.000Z",
            **({"committed": p["committed"]} if p.get("committed") else {}),
        }

    @staticmethod
    def plan_row(row):
        # Unobtainium matches no factor; everything else is Diesel at 2.68 kg/litre.
        if "unobtainium" in row["activity_name"].lower():
            return {"row_ref": row["row_ref"], "state": "issue", "input": row, "issues": [{"code": "UNKNOWN_ACTIVITY", "severity": "error", "message": f'"{row["activity_name"]}" matches no emission factor or mapping.', "field": "activity_name"}]}
        value = float(row.get("activity_value") or 1)
        return {
            "row_ref": row["row_ref"],
            "state": "ready",
            "input": row,
            "issues": [],
            "result": {
                "activity": {"name": row["activity_name"], "matched_by": "factor", "factor_name": "Diesel"},
                "factor": {"emission_factor_id": 1, "year": 2024, "value": 2680, "unit": "litre", "fallback_year": False},
                "activity_value": value,
                "activity_unit": row["activity_unit"],
                "total_tco2e": round(value * 2.68 / 1000, 6),
                "reporting_date": row["period"]["date"],
                "reporting_period": "monthly",
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "params": dict(request.url.params), "body": body, "auth": request.headers.get("authorization")})
        if self.fail and path.endswith(self.fail[0]):
            return httpx.Response(self.fail[1], json=self.fail[2])
        if request.method == "POST" and path == "/imports/plans":
            if len(body["rows"]) > plans.ROWS_PER_REQUEST:
                return httpx.Response(413, json={"code": "IMPORT_TOO_MANY_ROWS", "message": "Too many rows"})
            self.counter += 1
            plan_id = f"00000000-0000-4000-8000-{self.counter:012d}"
            self.plans[plan_id] = {"version": 1, "status": "draft", "rows": [self.plan_row(r) for r in body["rows"]], "source_name": body["source_name"], "company_id": body.get("company_id")}
            return httpx.Response(201, json={**self._plan(plan_id), "rows": self.plans[plan_id]["rows"][:50], "page": 1, "page_size": 50, "total_rows": len(body["rows"])})
        parts = path.split("/")
        plan_id = parts[3] if len(parts) > 3 else None
        if plan_id not in self.plans:
            return httpx.Response(404, json={"code": "IMPORT_PLAN_NOT_FOUND", "message": "No such plan."})
        p = self.plans[plan_id]
        if path.endswith("/rows"):
            if body["version"] != p["version"]:
                return httpx.Response(409, json={"code": "IMPORT_PLAN_VERSION_CONFLICT", "message": "Version moved.", "details": {"current_version": p["version"]}})
            p["rows"] += [self.plan_row(r) for r in body["rows"]]
            p["version"] += 1
            return httpx.Response(200, json=self._plan(plan_id))
        if path.endswith("/commit"):
            if body["version"] != p["version"]:
                return httpx.Response(409, json={"code": "IMPORT_PLAN_VERSION_CONFLICT", "message": "Version moved.", "details": {"current_version": p["version"]}})
            ready = [r for r in p["rows"] if r["state"] == "ready"]
            if not ready:
                return httpx.Response(400, json={"code": "IMPORT_VALIDATION", "message": "The plan has no ready rows to commit.", "details": {"ready": 0}})
            p["status"] = "committed"
            p["committed"] = {"upload_batch_id": f"batch-{plan_id[-3:]}", "inserted": len(ready), "fera_inserted": 0, "committed_at": "2026-01-01T00:00:00.000Z", "idempotency_key": body["idempotency_key"]}
            return httpx.Response(200, json=self._plan(plan_id))
        if path.endswith("/cancel"):
            p["status"] = "cancelled"
            return httpx.Response(200, json=self._plan(plan_id))
        if request.method == "GET":
            rows = p["rows"]
            state = request.url.params.get("state")
            if state:
                rows = [r for r in rows if r["state"] == state]
            page = int(request.url.params.get("page", 1))
            size = int(request.url.params.get("page_size", 50))
            return httpx.Response(200, json={**self._plan(plan_id), "rows": rows[(page - 1) * size : page * size], "page": page, "page_size": size, "total_rows": len(rows)})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "?"})


@pytest.fixture
def esg():
    return FakeEsgLite()


def client_for(esg, token="t0k3n"):
    return plans.PlanClient(token, base_url="http://esg-lite.test", transport=esg.transport())


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).fillna("")


# ---------------------------------------------------------------------------
# Sheet rows → ImportRows
# ---------------------------------------------------------------------------


def test_mapped_rows_become_import_rows():
    df = frame(
        [
            {"emission_category": "Diesel", "activity_value": "1,200", "activity_data_unit": "litre", "date_of_reporting": "15.01.2025", "Fuel Category": "Diesel", "extra_meter": "M-7"},
            {"emission_category": " Diesel ", "activity_value": 500, "activity_data_unit": "gallon", "date_of_reporting": "", "Fuel Category": "Diesel", "extra_meter": ""},
        ]
    )
    sheet = plans.rows_from_frame(df, [], SITE, CATEGORY, MONTH_END)
    assert sheet.local_issues == [] and sheet.total_rows == 2 and sheet.not_selected == 0
    assert sheet.rows == [
        {
            "row_ref": "Row 1",
            "site_id": 1,
            "category_id": 1,
            "period": {"kind": "monthly", "date": "2025-01-15"},  # day-first, as the sheet reader always read dates
            "activity_name": "Diesel",
            "activity_unit": "litre",
            "activity_value": 1200.0,
            "fields": {"Fuel Category": "Diesel", "meter": "M-7"},  # the site's own column stays; extra_ drops its prefix
        },
        {
            "row_ref": "Row 2",
            "site_id": 1,
            "category_id": 1,
            "period": {"kind": "monthly", "date": MONTH_END},  # no date cell: the upload's month end
            "activity_name": "Diesel",
            "activity_unit": "gallon",
            "activity_value": 500.0,
            "fields": {"Fuel Category": "Diesel"},
        },
    ]


def test_spec_rows_send_fields_not_a_value():
    df = frame([{"emission_category": "India Grid", "Calculation Method": "Direct energy", "Units Sold": "1,200", "Energy per use": "0.75", "Lifetime uses": "3650", "Share in use": "", "activity_data_unit": "kwh"}])
    sheet = plans.rows_from_frame(df, [], SITE, 3, MONTH_END)
    (row,) = sheet.rows
    assert "activity_value" not in row
    # ESG-lite parses the numbers (commas included), exactly as it does for the typed form.
    assert row["fields"] == {"Calculation Method": "Direct energy", "Units Sold": "1,200", "Energy per use": "0.75", "Lifetime uses": "3650"}


def test_rows_the_plans_api_would_refuse_become_local_issues():
    df = frame(
        [
            {"emission_category": "", "activity_value": "10", "activity_data_unit": "litre"},
            {"emission_category": "Diesel", "activity_value": "10", "activity_data_unit": ""},
            {"emission_category": "Diesel", "activity_value": "ten", "activity_data_unit": "litre"},
            {"emission_category": "Diesel", "activity_value": "", "activity_data_unit": "litre"},
            {"emission_category": "Diesel", "activity_value": "0", "activity_data_unit": "litre"},  # sent: ESG-lite answers NON_POSITIVE_VALUE
        ]
    )
    sheet = plans.rows_from_frame(df, [], SITE, CATEGORY, MONTH_END)
    assert [(i.row, i.code) for i in sheet.local_issues] == [(1, "MISSING_CATEGORY"), (2, "MISSING_UNIT"), (3, "VALUE_NOT_NUMERIC"), (4, "MISSING_VALUE")]
    assert [r["row_ref"] for r in sheet.rows] == ["Row 5"]
    assert sheet.rows[0]["activity_value"] == 0.0
    planned = sheet.local_issues[0].as_planned_row()
    assert planned["state"] == "issue" and planned["issues"][0]["severity"] == "error" and planned["local"] is True


def test_cells_over_esg_lites_text_limit_are_one_rows_issue_not_a_refused_request():
    """A 301-character cell would make ESG-lite's schema refuse the whole 2,000-row
    chunk; the sheet row becomes a TEXT_TOO_LONG local issue and the rest goes."""
    long = "x" * (plans.TEXT_MAX_CHARS + 1)
    df = frame(
        [
            {"emission_category": long, "activity_value": "10", "activity_data_unit": "litre"},
            {"emission_category": "Diesel", "activity_value": "10", "activity_data_unit": long},
            {"emission_category": "Diesel", "activity_value": "", "activity_data_unit": "litre", "extra_notes": long},
            {"emission_category": "Diesel", "activity_value": "", "activity_data_unit": "litre", "extra_" + "h" * (plans.FIELD_NAME_MAX_CHARS + 1): "ok"},
            {"emission_category": "Diesel", "activity_value": "10", "activity_data_unit": "litre", "extra_notes": "x" * plans.TEXT_MAX_CHARS},
        ]
    )
    sheet = plans.rows_from_frame(df, [], SITE, CATEGORY, MONTH_END)
    assert [(i.row, i.code) for i in sheet.local_issues] == [(1, "TEXT_TOO_LONG"), (2, "TEXT_TOO_LONG"), (3, "TEXT_TOO_LONG"), (4, "TEXT_TOO_LONG")]
    assert all(len(i.emission_category or "") <= plans.TEXT_MAX_CHARS for i in sheet.local_issues)
    assert [r["row_ref"] for r in sheet.rows] == ["Row 5"]


def test_ticked_categories_keep_sheet_row_numbers():
    df = frame([{"emission_category": c, "activity_value": "1", "activity_data_unit": "litre"} for c in ["Diesel", "Petrol", "Diesel"]])
    sheet = plans.rows_from_frame(df, ["Diesel"], SITE, CATEGORY, MONTH_END)
    assert sheet.total_rows == 3 and sheet.not_selected == 1
    assert [r["row_ref"] for r in sheet.rows] == ["Row 1", "Row 3"]


def test_a_sheet_over_the_plan_limit_is_refused_before_any_call():
    df = frame([{"emission_category": "Diesel", "activity_value": "1", "activity_data_unit": "litre"}] * (plans.ROWS_PER_PLAN + 1))
    with pytest.raises(ValueError, match="at most 20000"):
        plans.rows_from_frame(df, [], SITE, CATEGORY, MONTH_END)


# ---------------------------------------------------------------------------
# Plans: chunking, errors, preview and commit shapes
# ---------------------------------------------------------------------------


def rows(n, name="Diesel"):
    return [{"row_ref": f"Row {i + 1}", "site_id": SITE, "category_id": CATEGORY, "period": {"kind": "monthly", "date": MONTH_END}, "activity_name": name, "activity_unit": "litre", "activity_value": 100.0} for i in range(n)]


def test_large_sheets_go_in_chunks_following_the_version(esg):
    with client_for(esg) as c:
        plan = plans.create_plan_from_rows(c, "big.xlsx", rows(4500), company_id=7)
    calls = [(r["method"], r["path"], len(r["body"]["rows"]), r["body"].get("version")) for r in esg.requests]
    assert calls == [
        ("POST", "/imports/plans", 2000, None),
        ("POST", f"/imports/plans/{plan['plan_id']}/rows", 2000, 1),
        ("POST", f"/imports/plans/{plan['plan_id']}/rows", 500, 2),
    ]
    assert plan["version"] == 3 and plan["counts"]["rows"] == 4500
    first = esg.requests[0]
    assert first["auth"] == "Bearer t0k3n"
    assert first["body"]["channel"] == "bulk_upload" and first["body"]["company_id"] == 7 and first["body"]["source_name"] == "big.xlsx"


def test_esg_lite_errors_pass_through_with_status_code_and_details():
    esg = FakeEsgLite(fail=("/imports/plans", 422, {"code": "IMPORT_DUPLICATE_ROW_REF", "message": "Row refs repeat.", "details": {"row_refs": ["Row 2"]}}))
    with client_for(esg) as c, pytest.raises(plans.EsgLiteError) as err:
        plans.create_plan_from_rows(c, "x.xlsx", rows(2), None)
    assert (err.value.status, err.value.code, err.value.details) == (422, "IMPORT_DUPLICATE_ROW_REF", {"row_refs": ["Row 2"]})
    assert err.value.body() == {"code": "IMPORT_DUPLICATE_ROW_REF", "message": "Row refs repeat.", "details": {"row_refs": ["Row 2"]}}


def test_an_unreachable_esg_lite_is_a_502_not_a_crash():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    c = plans.PlanClient("t", base_url="http://esg-lite.test", transport=httpx.MockTransport(down))
    with pytest.raises(plans.EsgLiteError) as err:
        c.get_plan("p")
    assert err.value.status == 502 and err.value.code == "ESG_LITE_UNREACHABLE"


def test_preview_answers_the_plan_and_rows_in_the_sheet_shape(esg):
    df = frame(
        [
            {"emission_category": "Diesel", "activity_value": "1000", "activity_data_unit": "litre"},
            {"emission_category": "Unobtainium", "activity_value": "10", "activity_data_unit": "litre"},
            {"emission_category": "", "activity_value": "10", "activity_data_unit": "litre"},
        ]
    )
    sheet = plans.rows_from_frame(df, [], SITE, CATEGORY, MONTH_END)
    with client_for(esg) as c:
        out = plans.plan_sheet(c, sheet=sheet, source_name="fuel.csv", company_id=None, page=1, page_size=100, cancel_plan_id="00000000-0000-4000-8000-000000000099")
    # The earlier preview's plan is cancelled first (a miss is fine), then the new one is made and paged.
    assert [r["path"].split("/")[-1] for r in esg.requests] == ["cancel", "plans", esg.requests[2]["path"].split("/")[-1]]
    assert esg.requests[2]["method"] == "GET" and esg.requests[2]["params"] == {"page": "1", "page_size": "100"}
    plan = out["plan"]
    assert plan["plan_id"] and plan["version"] == 1 and plan["status"] == "draft"
    # The row this service could not send counts as an issue on top of ESG-lite's counts.
    assert plan["counts"] == {"rows": 3, "ready": 1, "issue": 2, "skipped": 0, "warnings": 0}
    assert plan["issues_summary"][-1] == {"code": "MISSING_CATEGORY", "count": 1, "example_row_refs": ["Row 3"], "local": True}
    assert out["total_rows"] == 3 and out["not_selected"] == 0 and out["sheet_rows"] == 3
    local, ready, issue = out["rows"]
    assert (local["row"], local["state"], local["row_error"]) == (3, "issue", "Missing emission category.")
    assert ready == {
        "row_ref": "Row 1",
        "row": 1,
        "state": "ready",
        "issues": [],
        "emission_category": "Diesel",
        "activity_value": 1000.0,
        "activity_data_unit": "litre",
        "global_category_name": "Diesel",
        "factor_value": 2680,
        "factor_year": 2024,
        "denominator_unit": "litre",
        "total_emission": 2.68,
        "fera_total_emission": None,
        "unit": "tCO2e",
        "date_of_reporting": MONTH_END,
    }
    assert issue["row"] == 2 and issue["state"] == "issue" and issue["total_emission"] is None
    assert issue["row_error"] == '"Unobtainium" matches no emission factor or mapping.'


def test_commit_answers_as_the_import_always_has(esg):
    df = frame(
        [
            {"emission_category": "Diesel", "activity_value": "1000", "activity_data_unit": "litre"},
            {"emission_category": "Unobtainium", "activity_value": "10", "activity_data_unit": "litre"},
            {"emission_category": "Diesel", "activity_value": "", "activity_data_unit": "litre"},
            {"emission_category": "Petrol", "activity_value": "5", "activity_data_unit": "litre"},
        ]
    )
    sheet = plans.rows_from_frame(df, ["Diesel", "Unobtainium"], SITE, CATEGORY, MONTH_END)
    with client_for(esg) as c:
        plan = plans.create_plan_from_rows(c, "fuel.csv", sheet.rows, None)
        res = plans.commit_plan(c, document_id=42, plan_id=plan["plan_id"], version=plan["version"], local_issues=sheet.local_issues, not_selected=sheet.not_selected, total_rows=sheet.total_rows)
    commit = next(r for r in esg.requests if r["path"].endswith("/commit"))
    assert commit["body"] == {"version": 1, "skip_rows_with_issues": True, "idempotency_key": f"bulk-upload:doc-42:plan-{plan['plan_id']}"}
    assert res["inserted"] == 1 and res["fera_inserted"] == 0 and res["skipped"] == 2 and res["not_selected"] == 1 and res["total_rows"] == 4
    assert res["upload_batch_id"] == f"batch-{plan['plan_id'][-3:]}" and res["plan_id"] == plan["plan_id"]
    assert res["skipped_rows"] == [
        {"row": 2, "emission_category": "Unobtainium", "reason": '"Unobtainium" matches no emission factor or mapping.'},
        {"row": 3, "emission_category": "Diesel", "reason": "No activity value or form fields in this row."},
    ]
    assert res["plan"]["status"] == "committed" and res["plan"]["committed"]["inserted"] == 1


def test_nothing_ready_means_no_batch(esg):
    with client_for(esg) as c:
        plan = plans.create_plan_from_rows(c, "fuel.csv", rows(2, "Unobtainium"), None)
        with pytest.raises(plans.EsgLiteError) as err:
            plans.commit_plan(c, document_id=1, plan_id=plan["plan_id"], version=plan["version"])
    assert err.value.status == 400 and err.value.code == "IMPORT_VALIDATION"


# ---------------------------------------------------------------------------
# The routes: sign-in, the document, the one-import-per-file claim
# ---------------------------------------------------------------------------

CSV = "Fuel,Qty,Unit\nDiesel,1000,litre\nUnobtainium,10,litre\n"
MAPPINGS = {"emission_category": "Fuel", "activity_value": "Qty", "activity_data_unit": "Unit"}
DOC, OTHER_DOC = 921, 922
MANAGER, SUPERADMIN, OUTSIDER = 2, 53, 54


def bearer(user_id, role):
    payload = {"userId": user_id, "role": role, "siteId": None, "exp": int(time.time()) + 3600}
    return {"Authorization": f"Bearer {jwt.encode(payload, os.environ['AUTH_JWT_SECRET'], algorithm='HS256')}"}


@pytest.fixture
def world(throwaway_db, monkeypatch):
    from app.core.database import ensure_uploaded_documents_table
    from app.services import excel_parser as ep

    ensure_uploaded_documents_table()
    from app.api import excel as excel_api

    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: (CSV.encode(), "csv"))
    deleted: list[tuple] = []
    monkeypatch.setattr(excel_api, "delete_document_file", lambda *a, **k: deleted.append((a, k)))
    with throwaway_db.cursor() as cur:
        cur.execute(
            """INSERT INTO "user" (user_id, name, email, password, role, site_id) VALUES
               (53, 'Root', 'root@example.invalid', 'x', 'Superadmin', NULL),
               (54, 'Nobody', 'nobody@example.invalid', 'x', 'Manager', NULL)"""
        )
        cur.execute("INSERT INTO uploaded_documents (id, document_name, cloudinary_url, uploaded_by) VALUES (%s, 'fuel-june.csv', 'stub', %s), (%s, 'other.csv', 'stub', %s)", (DOC, MANAGER, OTHER_DOC, SUPERADMIN))
        cur.execute("SELECT count(*) FROM emission")
        emissions_before = cur.fetchone()[0]
    yield {"deleted": deleted, "emissions_before": emissions_before}
    with throwaway_db.cursor() as cur:
        cur.execute("DELETE FROM uploaded_documents WHERE id IN (%s, %s)", (DOC, OTHER_DOC))
        cur.execute('DELETE FROM "user" WHERE user_id IN (53, 54)')


@pytest.fixture
def api(world, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    esg = FakeEsgLite()

    class WiredClient(plans.PlanClient):
        def __init__(self, token, **kw):
            super().__init__(token, base_url="http://esg-lite.test", transport=esg.transport())

    monkeypatch.setattr(plans, "PlanClient", WiredClient)
    return TestClient(app), esg


def body(**extra):
    return {"document_id": DOC, "mappings": MAPPINGS, "selected_categories": [], "site_id": SITE, "category_id": CATEGORY, "date_of_reporting": MONTH_END, **extra}


def test_preview_forwards_the_callers_token_and_names_the_client_for_a_superadmin(api):
    client, esg = api
    res = client.post("/v1/excel/preview", json=body(document_id=OTHER_DOC), headers=bearer(SUPERADMIN, "Superadmin"))
    assert res.status_code == 200, res.text
    create = next(r for r in esg.requests if r["method"] == "POST" and r["path"] == "/imports/plans")
    assert create["auth"] == bearer(SUPERADMIN, "Superadmin")["Authorization"]
    assert create["body"]["company_id"] == 1 and create["body"]["channel"] == "bulk_upload" and create["body"]["source_name"] == "other.csv"
    out = res.json()
    assert out["plan"]["counts"] == {"rows": 2, "ready": 1, "issue": 1, "skipped": 0, "warnings": 0}
    assert [r["row"] for r in out["rows"]] == [1, 2]


def test_a_manager_previews_their_own_site_without_a_company_id(api):
    client, esg = api
    res = client.post("/v1/excel/preview", json=body(), headers=bearer(MANAGER, "Manager"))
    assert res.status_code == 200, res.text
    create = next(r for r in esg.requests if r["method"] == "POST" and r["path"] == "/imports/plans")
    assert "company_id" not in create["body"]
    # Paging an existing plan makes no new plan.
    plan_id = res.json()["plan"]["plan_id"]
    again = client.post("/v1/excel/preview", json=body(plan_id=plan_id, page=1, page_size=1, state="issue"), headers=bearer(MANAGER, "Manager"))
    assert again.status_code == 200 and again.json()["rows"][0]["row"] == 2
    assert len([r for r in esg.requests if r["method"] == "POST" and r["path"] == "/imports/plans"]) == 1


def test_someone_elses_document_or_site_is_refused_before_any_call(api):
    client, esg = api
    assert client.post("/v1/excel/preview", json=body(document_id=OTHER_DOC), headers=bearer(MANAGER, "Manager")).status_code == 404
    assert client.post("/v1/excel/preview", json=body(), headers=bearer(OUTSIDER, "Manager")).status_code in (403, 404)
    assert client.post("/v1/excel/import", json=body(), headers=bearer(OUTSIDER, "Manager")).status_code in (403, 404)
    assert esg.requests == []


def test_import_commits_the_previewed_plan_once_and_writes_no_emission_rows(api, throwaway_db, world):
    client, esg = api
    headers = bearer(MANAGER, "Manager")
    plan = client.post("/v1/excel/preview", json=body(), headers=headers).json()["plan"]
    res = client.post("/v1/excel/import", json=body(plan_id=plan["plan_id"], version=plan["version"]), headers=headers)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["inserted"] == 1 and out["skipped"] == 1 and out["total_rows"] == 2 and out["upload_batch_id"]
    assert out["skipped_rows"] == [{"row": 2, "emission_category": "Unobtainium", "reason": '"Unobtainium" matches no emission factor or mapping.'}]
    commit = next(r for r in esg.requests if r["path"].endswith("/commit"))
    assert commit["auth"] == headers["Authorization"] and commit["body"]["idempotency_key"] == f"bulk-upload:doc-{DOC}:plan-{plan['plan_id']}"
    # The file is consumed, and this service inserted nothing itself.
    assert world["deleted"] == [((DOC,), {"new_status": "imported"})]
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM emission")
        assert cur.fetchone()[0] == world["emissions_before"]
        cur.execute("SELECT status FROM uploaded_documents WHERE id = %s", (DOC,))
        assert cur.fetchone()[0] == "importing"  # delete_document_file (stubbed) is what flips it to imported
    # A second import of the same file is refused.
    again = client.post("/v1/excel/import", json=body(plan_id=plan["plan_id"], version=plan["version"]), headers=headers)
    assert again.status_code == 409


def test_a_failed_file_delete_still_marks_the_document_imported(world, throwaway_db, monkeypatch):
    """The rows are on ESG-lite by the time the file is deleted, so a storage
    hiccup must not leave the document "importing" (a retry would then be
    refused as already being imported for a batch that landed)."""
    from app.services import excel_parser as ep

    def boom(public_id):
        raise RuntimeError("storage down")

    monkeypatch.setattr(ep.cloudinary_service, "delete_file", boom)
    with throwaway_db.cursor() as cur:
        cur.execute("UPDATE uploaded_documents SET cloudinary_public_id = 'asset-1', status = 'importing' WHERE id = %s", (DOC,))
    ep.delete_document_file(DOC, new_status="imported")
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT status FROM uploaded_documents WHERE id = %s", (DOC,))
        assert cur.fetchone()[0] == "imported"


def test_import_without_a_plan_plans_then_commits(api):
    client, esg = api
    res = client.post("/v1/excel/import", json=body(), headers=bearer(MANAGER, "Manager"))
    assert res.status_code == 200, res.text
    assert [r["path"].split("/")[-1] for r in esg.requests][:2] == ["plans", "commit"]


def test_a_failed_commit_hands_the_file_back(api, throwaway_db, world, monkeypatch):
    client, esg = api
    headers = bearer(MANAGER, "Manager")
    plan = client.post("/v1/excel/preview", json=body(), headers=headers).json()["plan"]
    esg.fail = ("/commit", 409, {"code": "IMPORT_PLAN_VERSION_CONFLICT", "message": "Rows changed on re-check.", "details": {"reason": "rows changed on re-check", "rows": []}})
    res = client.post("/v1/excel/import", json=body(plan_id=plan["plan_id"], version=plan["version"]), headers=headers)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "IMPORT_PLAN_VERSION_CONFLICT"
    with throwaway_db.cursor() as cur:
        cur.execute("SELECT status FROM uploaded_documents WHERE id = %s", (DOC,))
        assert cur.fetchone()[0] == "uploaded"
    assert world["deleted"] == []
    esg.fail = None
    assert client.post("/v1/excel/import", json=body(plan_id=plan["plan_id"], version=plan["version"]), headers=headers).status_code == 200
